"""data内のLP512 Excelを検出し、ファイル・シートごとの設定を管理する。"""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

import openpyxl
from openpyxl.utils.exceptions import InvalidFileException

from calculation import DEFAULT_PARAMETERS as PHYSICAL_PARAMETERS
from measurement import ROOT
from csv_import import convert_csv

DEFAULT_CONFIGS = (ROOT/'analysis_config.json', ROOT/'earth_analysis_config.json')
DEFAULT_PARAMETERS = PHYSICAL_PARAMETERS | {'resistance_ohm':1e6}
HEADERS = {0:'index',5:'DAC_A_nominal_V',7:'Vout_A_nominal_V'}


def excel_files(data_dir):
    directory=Path(data_dir)
    if not directory.is_dir():return []
    return input_files(directory,('.xlsx',))


def input_files(directory,suffixes=('.xlsx','.csv')):
    directory=Path(directory)
    if not directory.is_dir():return []
    return sorted((p for p in directory.rglob('*') if p.is_file() and p.suffix.lower() in suffixes
                   and not p.name.startswith('~$') and '.converted' not in p.relative_to(directory).parts),
                  key=lambda p:str(p).casefold())


def data_snapshot(data_dir):
    result=[]
    directory=Path(data_dir)
    for path in input_files(directory):
        try:
            info=path.stat()
            result.append((path.relative_to(directory).as_posix(),info.st_size,info.st_mtime_ns))
        except OSError:
            continue
    return tuple(result)


def finite_number(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def initial_config(source, sheet_name, records, config_path):
    """既存の補正表の範囲、なければ最初の適用可能な掃引を表示範囲にする。"""
    def at(row,column):
        if row>len(records) or column>len(records[row-1]):return None
        return records[row-1][column-1]

    config=copy.deepcopy(DEFAULT_PARAMETERS)
    lower,upper=-10.,10.
    if lower>=upper:raise ValueError('適用判定の下限が上限以上です。')
    valid={}
    for raw,row in enumerate(records[1:],start=2):
        if len(row)<8 or not finite_number(row[0]) or not finite_number(row[5]) or not finite_number(row[7]):continue
        total=-float(row[5])-float(row[7])
        if lower<total<upper:valid[raw+1]=str(row[1] or '')
    corrected=[]
    for row in range(2,len(records)+1):
        formula=at(row,31)
        if (row in valid and isinstance(formula,str) and
                re.match(fr'^=\s*P\$?{row}\s*[-+*/(]',formula,re.IGNORECASE)
                and re.search(fr'\bO\$?{row}\b',formula,re.IGNORECASE)):
            corrected.append(row)
    # 表示範囲を既存補正表から読み取る。近似領域・Vfはここでは選択しない。
    candidates=corrected or list(valid)
    runs=[]
    for row in candidates:
        if runs and row==runs[-1][-1]+1 and valid[row]==valid[runs[-1][-1]]:runs[-1].append(row)
        else:runs.append([row])
    runs=[run for run in runs if len(run)>=2]
    if not runs:raise ValueError('適用判定内の連続する測定点が2点以上ありません。')
    forward=[run for run in runs if valid[run[0]].casefold()=='forward']
    chosen=max(forward or runs,key=len)
    first,last=chosen[0],chosen[-1]
    identity=profile_identity(source,sheet_name)
    vf_row=max(154,last+3)
    config.update({
        'input':Path(os.path.relpath(source,config_path.parent)).as_posix(),
        'output_dir':Path(os.path.relpath(config_path.parent.parent/'outputs'/identity,config_path.parent)).as_posix(),
        'sheet':sheet_name,'analysis_rows':[first,last],'ion_equation_cell':'AK121',
        'retarding_fit_rows':None,'saturation_fit_rows':None,'vp_fit_rows':None,
        'analysis_protocol':'lp24',
        'ies_method':'vp_neighbors','floating_potential_mode':'unselected','floating_potential_rows':None,
        'applicability_min_V':lower,'applicability_max_V':upper,
        'vf_output_cell':f'AE{vf_row}','log_min_output_cell':f'AF{vf_row}',
    })
    return config


def profile_identity(source,sheet_name):
    source=Path(source).resolve()
    digest=hashlib.sha256((str(source).casefold()+'\0'+sheet_name).encode('utf-8')).hexdigest()[:12]
    stem=re.sub(r'[^\w.-]+','_',source.stem).strip('. ')[:48] or 'measurement'
    return f'data_{stem}_{digest}'


def discover_profiles(data_dir=None,config_dir=None,known_configs=None):
    """戻り値は表示名→設定パスと読み込みエラー。入力Excelを変更しない。"""
    data_dir=Path(data_dir or ROOT/'data').resolve()
    config_dir=Path(config_dir or ROOT/'profiles').resolve()
    known_configs=list(DEFAULT_CONFIGS if known_configs is None else known_configs)
    saved_configs=sorted(config_dir.glob('data_*.json')) if config_dir.exists() else []
    known={};errors=[]
    for path in saved_configs+list(map(Path,known_configs)):
        if not path.exists():continue
        try:
            settings=json.loads(path.read_text(encoding='utf-8-sig'))
            source=(path.parent/settings['input']).resolve()
            known[(source,settings['sheet'])]=path.resolve()
        except (OSError,ValueError,KeyError,TypeError) as error:
            errors.append(f'{path.name}: 設定を読み込めません（{error}）')
    profiles={};found_keys=set()
    inputs=[(path,path.relative_to(data_dir).as_posix()) for path in excel_files(data_dir)]
    for path in input_files(data_dir,('.csv',)):
        if path.is_file():
            try:
                inputs.append((convert_csv(path),f'{path.relative_to(data_dir).as_posix()} → Excel'))
            except (OSError,ValueError,csv.Error) as error:
                errors.append(f'{path.name}: {error}')
    for source,display_name in inputs:
        workbook=None
        try:
            workbook=openpyxl.load_workbook(source,read_only=True,data_only=False)
            sheets=[]
            for sheet in workbook:
                header=next(sheet.iter_rows(min_row=1,max_row=1,max_col=8,values_only=True),())
                if len(header)<8 or any(header[i]!=label for i,label in HEADERS.items()):continue
                sheets.append(sheet)
            if not sheets:
                errors.append(f'{source.name}: LP512形式のシートがありません。')
            for sheet in sheets:
                key=(source.resolve(),sheet.title)
                config_path=known.get(key)
                if config_path is None:
                    config_path=config_dir/(profile_identity(source,sheet.title)+'.json')
                    if not config_path.exists():
                        try:
                            records=list(sheet.iter_rows(max_col=43,values_only=True))
                            settings=initial_config(source.resolve(),sheet.title,records,config_path)
                            config_dir.mkdir(parents=True,exist_ok=True)
                            with config_path.open('x',encoding='utf-8') as file:
                                file.write(json.dumps(settings,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
                        except FileExistsError:
                            pass
                        except (OSError,ValueError,TypeError) as error:
                            errors.append(f'{source.name} / {sheet.title}: {error}')
                            continue
                label=display_name if len(sheets)==1 else f'{display_name} / {sheet.title}'
                profiles[label]=config_path
                found_keys.add(key)
        except (OSError,ValueError,KeyError,TypeError,BadZipFile,InvalidFileException,ParseError) as error:
            # コピー中・破損したExcelも、ほかのファイルの検出を止めない。
            errors.append(f'{source.name}: 読み込めません（{error}）')
        finally:
            if workbook is not None:workbook.close()
    # --configで直接開いた設定も、data外の入力なら一覧に保持する。
    for key,path in known.items():
        source,sheet=key
        if key not in found_keys and source.exists() and not source.is_relative_to(data_dir):
            label=f'{source.name} / {sheet}'
            profiles[label]=path
    return profiles,errors

"""LP測定CSVを、元CSVを保存したまま解析用Excelへ変換する。"""
from __future__ import annotations

import csv
import hashlib
import io
from pathlib import Path

import openpyxl


def converted_path(source: Path) -> Path:
    source = Path(source).resolve()
    identity = hashlib.sha256(source.name.encode('utf-8')).hexdigest()[:12]
    return source.parent / '.converted' / f'{source.stem}_{identity}.xlsx'


def convert_csv(source: Path) -> Path:
    source = Path(source).resolve()
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    destination = converted_path(source)
    stamp = destination.with_suffix('.sha256')
    if destination.exists() and stamp.exists() and stamp.read_text() == digest:
        return destination
    for encoding in ('utf-8-sig', 'cp932', 'utf-16'):
        try:
            contents = payload.decode(encoding)
            break
        except UnicodeError:
            continue
    else:
        raise ValueError('CSVの文字コードを読み取れません。UTF-8またはShift-JISで保存してください。')
    try:
        dialect = csv.Sniffer().sniff(contents[:8192], delimiters=',\t;')
    except csv.Error:
        dialect = csv.excel
    records = list(csv.reader(io.StringIO(contents), dialect))
    if not records:
        raise ValueError('CSVが空です。')
    header = [value.strip() for value in records[0]]
    required = ('DAC_A_nominal_V', 'Vout_A_nominal_V')
    if any(name not in header for name in required):
        raise ValueError('CSVにDAC_A_nominal_VとVout_A_nominal_Vの列が必要です。')
    # 入力列の並びに依存せず、既存Excelと同じF/H列へ配置する。
    canonical = ['index', 'sweep', 'DAC_B_nominal_V', 'Ipol_B_estimated_A',
                 'Vout_B_nominal_V', 'DAC_A_nominal_V', 'Ipol_A_estimated_A',
                 'Vout_A_nominal_V', 'resistance_ohm', 'profile_id', 'board_status',
                 'resistance_A_ohm', 'resistance_B_ohm']
    columns = canonical + [name for name in header if name not in canonical]
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = 'LP_data'
    sheet.append(columns)
    for cell in sheet[1]:cell.data_type='s'
    for index, record in enumerate(records[1:]):
        if not any(value.strip() for value in record):
            continue
        values = dict(zip(header, record))
        row = []
        for name in columns:
            value = values.get(name, index if name == 'index' else None)
            if isinstance(value, str):
                value = value.strip()
                try:
                    value = float(value)
                except ValueError:
                    pass
            row.append(value)
        sheet.append(row)
        # CSV文字列をExcelの数式として実行しない。
        for cell in sheet[sheet.max_row]:
            if isinstance(cell.value, str):
                cell.data_type = 's'
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.tmp.xlsx')
    try:
        book.save(temporary)
        temporary.replace(destination)
        stamp.write_text(digest)
    finally:
        book.close()
        if temporary.exists():
            temporary.unlink()
    return destination

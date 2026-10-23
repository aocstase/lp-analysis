"""data内のCSVを直接検出し、測定ファイルごとの設定を管理する。"""
import csv
import hashlib
import json
import os
from pathlib import Path
import re

from lp_calculation import ROOT, normalize_config, select_applicable_rows

DEFAULT_CONFIGS = (ROOT / 'analysis_config.json', ROOT / 'earth_analysis_config.json')


def input_files(directory):
    directory = Path(directory)
    if not directory.is_dir():
        return []
    return sorted((path for path in directory.rglob('*')
                   if path.is_file() and path.suffix.lower() == '.csv'
                   and not any(part.startswith('.') for part in path.relative_to(directory).parts)),
                  key=lambda path: str(path).casefold())


def data_snapshot(data_dir):
    snapshot = []
    for path in input_files(data_dir):
        try:
            info = path.stat()
            snapshot.append((path.relative_to(data_dir).as_posix(), info.st_size, info.st_mtime_ns))
        except OSError:
            continue
    return tuple(snapshot)


def profile_identity(source):
    source = Path(source).resolve()
    digest = hashlib.sha256(str(source).casefold().encode('utf-8')).hexdigest()[:12]
    stem = re.sub(r'[^\w.-]+', '_', source.stem).strip('. ')[:48] or 'measurement'
    return f'data_{stem}_{digest}'


def initial_config(source, config_path):
    """適用内の最長の順方向掃引を選ぶ。Vf方法・近似領域は利用者が指定する。"""
    config = normalize_config({})
    config.update(input=Path(os.path.relpath(source, config_path.parent)).as_posix(),
                  output_dir=Path(os.path.relpath(config_path.parent.parent / 'outputs' / config_path.stem,
                                                 config_path.parent)).as_posix(),
                  floating_potential_mode='unselected', retarding_fit_rows=None, saturation_fit_rows=None)
    config['analysis_rows'] = select_applicable_rows(source, config)
    return config


def discover_profiles(data_dir=None, config_dir=None, known_configs=None):
    """CSVを登録する。既存のVf方法・近似範囲は保持し、不正なCSVは個別に報告する。"""
    data_dir, config_dir = Path(data_dir or ROOT / 'data').resolve(), Path(config_dir or ROOT / 'profiles').resolve()
    paths = [*sorted(config_dir.glob('data_*.json')), *map(Path, DEFAULT_CONFIGS if known_configs is None else known_configs)]
    known, profiles, errors = {}, {}, []
    for path in paths:
        if not path.is_file():
            continue
        try:
            config = json.loads(path.read_text(encoding='utf-8-sig'))
            source = (path.parent / config['input']).resolve()
            if source.suffix.lower() == '.csv':
                known[source] = path.resolve()
        except (OSError, ValueError, KeyError, TypeError) as error:
            errors.append(f'{path.name}: 設定を読み込めません（{error}）')
    for source in dict.fromkeys([*input_files(data_dir), *(p for p in known if not p.is_relative_to(data_dir) and p.is_file())]):
        try:
            path = known.get(source, config_dir / f'{profile_identity(source)}.json')
            if not path.exists():
                config = initial_config(source, path)
                config_dir.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(config, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
            label = source.relative_to(data_dir).as_posix() if source.is_relative_to(data_dir) else source.name
            profiles[label] = path
        except (OSError, ValueError, KeyError, TypeError, csv.Error) as error:
            errors.append(f'{source.name}: {error}')
    return profiles, errors

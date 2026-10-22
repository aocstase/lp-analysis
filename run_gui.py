"""GUI版の入口。CSVを取り込み、設定の原本を保ちながら選択結果を保存する。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

from csv_import import convert_csv
from lp_calculation import load_config, normalize_config

ROOT = Path(__file__).resolve().parent


def prepare_gui_profile(config_path, *, root=None):
    """入力CSVをExcelへ変換。GUI専用の設定をprofilesに作り、選択済みの値を保つ。"""
    root = Path(root or ROOT).resolve()
    config_path = Path(config_path).resolve()
    config = normalize_config(load_config(config_path))
    source = (config_path.parent / config['input']).resolve()
    if source.suffix.lower() == '.csv':
        source = convert_csv(source)
        config['sheet'] = 'LP_data'
    elif source.suffix.lower() == '.xlsx':
        if not config.get('sheet'):
            import openpyxl
            book = openpyxl.load_workbook(source, read_only=True)
            try:config['sheet'] = book.sheetnames[0]
            finally:book.close()
    else:
        raise ValueError('入力はLP512形式の.csvまたは.xlsxにしてください。')

    identity = hashlib.sha256((str(config_path) + '\0' + str(source) + '\0' + config['sheet']).encode('utf-8')).hexdigest()[:12]
    profile = root / 'profiles' / f'data_gui_{identity}.json'
    if profile.exists():
        # 前回GUIで選んだVf方法・領域・物理定数を、原本の値で上書きしない。
        return profile
    config['input'] = Path(os.path.relpath(source, profile.parent)).as_posix()
    output = ((config_path.parent / config['output_dir']).resolve() if config.get('output_dir')
              else root / 'outputs' / f'gui_{identity}')
    config['output_dir'] = Path(os.path.relpath(output, profile.parent)).as_posix()
    # 長いCSVでも、Vf出力欄を解析データの後ろに置いて衝突させない。
    vf_row = max(154, config['analysis_rows'][1] + 3)
    config.setdefault('vf_output_cell', f'AE{vf_row}')
    config.setdefault('log_min_output_cell', f'AF{vf_row}')
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(json.dumps(config, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    return profile


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'settings.json', help='初期表示する測定の設定JSON')
    args = parser.parse_args(argv)
    try:
        profile = prepare_gui_profile(args.config)
        import tkinter as tk
        from manual_analysis import ManualAnalysisApp
        root = tk.Tk()
        ManualAnalysisApp(root, profile)
        root.mainloop()
        return 0
    except (OSError, ValueError, KeyError, TypeError, ImportError) as error:
        print(f'GUI起動エラー: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

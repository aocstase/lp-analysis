"""CSV解析GUIの起動。元の設定を保持し、GUIで選んだ条件を個別に保存する。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

from lp_calculation import ROOT, load_config, normalize_config


def prepare_gui_profile(config_path, *, root=None):
    root = Path(root or ROOT).resolve()
    config_path = Path(config_path).resolve()
    config = normalize_config(load_config(config_path))
    source = (config_path.parent / config['input']).resolve()
    if source.suffix.lower() != '.csv' or not source.is_file():
        raise ValueError('入力CSVを指定してください。')
    identity = hashlib.sha256((str(config_path) + '\0' + str(source)).encode('utf-8')).hexdigest()[:12]
    profile = root / 'profiles' / f'data_gui_{identity}.json'
    if profile.exists():
        return profile
    config['input'] = Path(os.path.relpath(source, profile.parent)).as_posix()
    output = ((config_path.parent / config['output_dir']).resolve() if config.get('output_dir')
              else root / 'outputs' / f'gui_{identity}')
    config['output_dir'] = Path(os.path.relpath(output, profile.parent)).as_posix()
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(json.dumps(config, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    return profile


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    default = ROOT / ('settings.json' if (ROOT / 'settings.json').exists() else 'analysis_config.json')
    parser.add_argument('--config', type=Path, default=default)
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

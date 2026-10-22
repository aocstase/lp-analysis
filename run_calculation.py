"""既存プロジェクト向けの起動口。単独で持ち運ぶ場合はlp_calculation.pyを使う。

従来どおり--no-exportで表示のみ、指定なしでExcel・画像も保存する。
"""
from lp_calculation import ROOT, load_config
from lp_calculation import choose_config as _choose_config, main as _main


def choose_config(explicit=None):
    return _choose_config(explicit, root=ROOT)


def refresh_csv(source):
    """既存のCSV変換キャッシュを更新する。単独版ではCSVを直接読む。"""
    from csv_import import converted_path, convert_csv
    if source.suffix.lower() == '.csv':
        return convert_csv(source)
    if source.parent.name == '.converted':
        for original in source.parent.parent.iterdir():
            if original.suffix.lower() == '.csv' and converted_path(original) == source:
                return convert_csv(original)
    return source


def export_project_results(source, config, out):
    from result_output import export_analysis
    export_analysis(source, config, out)


def main(argv=None):
    return _main(argv, root=ROOT, prepare_source=refresh_csv, exporter=export_project_results)


if __name__ == '__main__':
    raise SystemExit(main())

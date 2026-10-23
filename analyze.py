"""GUIと既存起動コマンドの入口。数値計算はlp_calculation.pyに集約する。"""
from lp_calculation import ROOT, Calculation as Analysis, calculate as analyze
from result_output import export_analysis


def main(argv=None):
    from run_calculation import main as run
    return run(argv)


if __name__ == '__main__':
    raise SystemExit(main())

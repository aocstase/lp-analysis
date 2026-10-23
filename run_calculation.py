"""CSVを直接解析する起動口。--no-exportで画面・保存なしの計算もできる。"""
from lp_calculation import ROOT, choose_config, load_config
from lp_calculation import main as run


def main(argv=None):
    return run(argv, root=ROOT)


if __name__ == '__main__':
    raise SystemExit(main())

"""GUIからも計算専用版と同じCSV・PNG・JSONを保存する。"""
from lp_calculation import calculate, save_results


def export_analysis(source, config, out, result=None):
    result = result if result is not None else calculate(source, config)
    save_results(out, result, source, config, None)
    return result

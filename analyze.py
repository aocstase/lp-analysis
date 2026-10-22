"""LP解析の入口。測定読込・数値計算・保存をつなぐ。

数式の解説は lp_calculation.py、Excelの書式を保つ処理は excel_output.py にまとめる。
GUIと画面なし実行は、どちらも同じanalyze()を使う。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from calculation import (
    Fit, ROUNDOFF_FACTOR, calculate_parameters, correct_trace,
    fit_line, fit_vp_neighbors, intersect_fits, normalize_config,
)
# 従来のスクリプトからのimport名を保つため、保存関数もここから公開する。
from excel_output import NS, corrected_formula, prepare_excel_result, range_values, write_workbook
from floating_potential import FloatingPotential
from measurement import ROOT, load_measurement
from result_output import export_analysis


@dataclass
class Analysis:
    """計算結果の入れ物。画面と各出力は同じ結果を参照する。"""
    sheet: str
    values: dict
    formulas: dict
    all_rows: list[int]
    rows: np.ndarray
    voltage: np.ndarray
    current: np.ndarray
    ion_current: np.ndarray
    electron_current: np.ndarray
    log_current: np.ndarray
    derivative: np.ndarray
    fits: dict[str, Fit]
    metrics: dict[str, float]
    notes: list[str]
    original_formulas: dict
    floating_potential: FloatingPotential
    text_updates: dict = field(default_factory=dict)
    added_cells: set = field(default_factory=set)
    vf_output_cell: str = "AE154"
    log_min_output_cell: str = "AF154"


def analyze(source, config) -> Analysis:
    """保存済みの測定範囲・Vf方法・減速/飽和領域を使って解析する。"""
    # 1. Excelの保存済み数式結果ではなく、生データからVout・Ipolを変換する。
    config = normalize_config(config)
    measurement = load_measurement(source, config)
    rows, x, y = measurement.rows, measurement.voltage, measurement.current

    # 2. 負電圧からイオン直線を計算し、差し引き・ln変換・Vp0の差分を求める。
    trace = correct_trace(x, y, rows)

    # 3. Vfと選択した2領域からVpを求め、Vp前後の自動近似でIes・Te・密度を求める。
    fits, metrics, floating = calculate_parameters(x, rows, trace, config)
    notes = [
        "Vout=-Vout_A、Ipol=-DAC_A/10^6 [A]。-10<Ipol*10^6+Vout<10が適用範囲。",
        "負電圧の全点からイオン直線を毎回計算し、符号付き切片を含めて差し引く。",
        "差し引きの丸め誤差は32*機械イプシロン*(abs(Ipol)+abs(a*Vout)+abs(b))以下なら0。",
        "ln(abs(Ie))を計算。0 Aは未定義のため空欄。グラフとVf候補はVout>0のみ。",
        "Vfは目視で選んだ方法を使用: ln最小実測点、またはln値の小さい2点の電圧平均。",
        "Vp0はabs(隣接ΔIe/隣接ΔV)の最大区間の先頭点。同率なら最初。末尾差分は空欄。",
        "減速領域はVf<=Vout<Vp0。飽和領域にVp0による制限はない。両領域は手動指定。",
        "Vpは減速・飽和のln直線の交点。その前後の最寄り点からIesを自動評価する。",
        "Ies=exp(局所直線のVp代入値) [A]。Te=e/(k*減速傾き)、球面積S=4*pi*r^2。",
        "平均速度v=sqrt(8*k*Te/(pi*me))、ne=4*Ies/(e*S*v) [m^-3]。10^6で割ってcm^-3へ換算。",
    ]
    for name, fit in fits.items():
        if fit.points == 2:
            notes.append(f"{name}は2点近似。R²=1だけでは近似の妥当性を評価できない。")

    result = Analysis(
        measurement.sheet, dict(measurement.values), {}, measurement.all_rows,
        rows, x, y, trace.ion_current, trace.electron_current, trace.log_current,
        trace.derivative, fits, metrics, notes, measurement.original_formulas, floating,
    )
    # 4. Excel再計算用の式も用意する。ここでは入力ファイルを上書きしない。
    return prepare_excel_result(result, measurement, config)


def main(argv=None):
    """従来のpython analyze.py / run_project.py --batchからも実行できる。"""
    from run_calculation import main as run
    return run(argv)


if __name__ == "__main__":
    raise SystemExit(main())

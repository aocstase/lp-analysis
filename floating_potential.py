"""Vfの互換窓口。現行計算はlp_calculation.py、旧Excel再現用の処理だけ残す。"""
import math
from lp_calculation import FloatingPotential, minimum_floating_potential


def estimate_floating_potential(voltage, log_current, excel_rows, selected_rows=None):
    x, y, rows = list(map(float, voltage)), list(map(float, log_current)), list(map(int, excel_rows))
    if len(x) != len(y) or len(x) != len(rows) or len(x) < 2:
        raise ValueError('Vf: 同じ長さの電圧・ln表示・行番号が2点以上必要です。')
    if len(set(rows)) != len(rows) or not all(math.isfinite(v) for v in x+y):
        raise ValueError('Vf: 行番号は一意、電圧とln表示は有限値にしてください。')
    i = min(range(len(y)), key=y.__getitem__)
    if selected_rows is None:
        neighbors = [j for j in (i-1, i+1) if 0 <= j < len(y)]
        j = min(neighbors, key=lambda j: (y[j], rows[j]))
        left, right = sorted((i, j))
        method = 'minimum_log_and_lower_log_neighbor_midpoint'
    else:
        if len(selected_rows) != 2 or any(r not in rows for r in selected_rows):
            raise ValueError('Vf: 指定する2行は解析範囲内にしてください。')
        left, right = sorted(rows.index(r) for r in selected_rows)
        if right-left != 1 or rows[right]-rows[left] != 1:
            raise ValueError('Vf: 隣接する2行を指定してください。')
        method = 'specified_adjacent_voltage_midpoint'
    return FloatingPotential((x[left]+x[right])/2, (rows[left], rows[right]),
                             (x[left], x[right]), rows[i], x[i], y[i], method)


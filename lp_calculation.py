"""球状ラングミュアプローブの解析。Python 3.10以上で単独実行できる。

python lp_calculation.py --config settings.json
python lp_calculation.py --config settings.json --output-dir results
python lp_calculation.py --input sample.csv --analysis-rows 1 53 --vf-mode minimum --retarding-rows 25 37 --saturation-rows 49 53

必要ライブラリ: numpy、matplotlib（グラフ保存）。入力はLP512形式のCSV。
構成: 設定・測定値の読込 → 数値計算 → CSV・PNG保存 → ターミナル操作。
計算の入口はcalculate、実行の入口はmain。GUIも同じ計算関数を使う。
rowsはCSVのデータ行（見出しを除き1から数える）、xはVout[V]、yは補正前のIpol[A]。
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
import io
import json
import math
from pathlib import Path
import sys
from typing import NamedTuple

import numpy as np


# 定数・結果の入れ物


ROOT = Path(__file__).resolve().parent

# 物理定数と球半径の初期値。イオン近似の係数は固定せず、測定値から求める。
DEFAULT_PARAMETERS = {
    "probe_radius_m": 0.02,
    "electron_mass_kg": 9.10939e-31,
    "boltzmann_J_per_K": 1.38e-23,
    "electron_charge_C": 1.6e-19,
}
RESISTANCE_OHM = 1e6
ROUNDOFF_FACTOR = 32 * np.finfo(float).eps


@dataclass(frozen=True)
class Fit:
    """y = slope*x + intercept と、使用した行・点数・決定係数R²。"""
    slope: float
    intercept: float
    r_squared: float | None
    rows: tuple[int, int]
    points: int

    def at(self, x):
        return self.slope * x + self.intercept


class Trace(NamedTuple):
    """イオン補正後の配列。NaNはln・差分を定義できない点を表す。"""
    ion_current: np.ndarray
    electron_current: np.ndarray
    log_current: np.ndarray
    derivative: np.ndarray
    ion_fit: Fit
    peak_index: int


@dataclass(frozen=True)
class FloatingPotential:
    """浮遊電位と、その決定に使ったCSVのデータ行・電圧の記録。"""
    voltage_V: float
    rows: tuple[int, ...]
    endpoint_voltages_V: tuple[float, ...]
    minimum_row: int
    minimum_voltage_V: float
    minimum_log_current: float
    method: str

    def to_dict(self):
        return asdict(self)


@dataclass
class Calculation:
    """GUIと計算専用版で共有する結果。単位はV・A・K・m^-3。"""
    rows: np.ndarray
    voltage: np.ndarray
    current: np.ndarray
    trace: Trace
    fits: dict[str, Fit]
    metrics: dict[str, float]
    floating_potential: FloatingPotential
    all_points: tuple | None = None
    all_resistance: np.ndarray | None = None
    applicability_bounds: tuple = (-10., 10.)


# 設定・測定値の読込（手順1–7）


def resistance_value(value, fallback=RESISTANCE_OHM):
    """L列のA側抵抗[Ω]を優先する。空欄の場合だけ設定値（標準1 MΩ）を使う。"""
    if value is None or value == '':
        value = fallback
    try:
        resistance = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError('A側抵抗 resistance_A_ohm は正の数値[Ω]にしてください。') from error
    if isinstance(value, bool) or not math.isfinite(resistance) or resistance <= 0:
        raise ValueError('A側抵抗 resistance_A_ohm は正の有限値[Ω]にしてください。')
    return resistance


def applicability_bounds(config):
    """適用判定は下限 < Vin+Vout < 上限。境界上の点は含めない。"""
    lower = float(config.get('applicability_min_V', -10))
    upper = float(config.get('applicability_max_V', 10))
    if not math.isfinite(lower) or not math.isfinite(upper) or lower >= upper:
        raise ValueError('適用範囲の下限・上限は有限値で、下限 < 上限にしてください。')
    return lower, upper


def normalize_config(config):
    """省略された定数と上下限を補う。近似係数や領域は測定値・利用者の指定を使う。"""
    settings = DEFAULT_PARAMETERS | dict(config)
    settings['resistance_ohm'] = resistance_value(settings.get('resistance_ohm'))
    lower, upper = applicability_bounds(settings)
    settings.update(applicability_min_V=lower, applicability_max_V=upper)
    settings['row_numbering'] = 'csv_data_1_based'
    return settings


def load_config(path):
    """パスは設定JSONのあるフォルダを基準にするため、PCのユーザー名に依存しない。"""
    path = Path(path).resolve()
    if path.suffix.lower() != '.json':
        raise ValueError('設定JSONを指定してください。')
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(config, dict) or not all(key in config for key in ('input', 'analysis_rows')):
        raise ValueError('設定JSONにはinput・analysis_rowsが必要です。')
    return config


def read_points(source, config, *, include_all=False, metadata=False, validate_selection=True):
    """LP512のCSVを直接読む。空行を除いた最初のデータを1行目とする。"""
    source = Path(source)
    points = []

    fallback = resistance_value(config.get('resistance_ohm'))

    def append(analysis_row, dac, vout, resistance=None, sweep=''):
        for value, label in ((dac, 'DAC_A_nominal_V'), (vout, 'Vout_A_nominal_V')):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f'{analysis_row}行の{label}には有限な数値が必要です。')
        # 手順2–3: CSVの列名でVoutとDACを取得し、Vout=-Vout_A、Ipol=-DAC_A/R。
        # 手順3と5には必ず同じ抵抗を使う。500 kΩなら5×10^5 Ω。
        resistance = resistance_value(resistance, fallback)
        points.append((analysis_row, -float(vout), -float(dac) / resistance, resistance, str(sweep or '')))

    if source.suffix.lower() == '.csv':
        payload = source.read_bytes()
        # BOM付きUTF-16は先に判定する。Shift-JISとして誤読して列名が壊れるのを防ぐ。
        encodings = ('utf-16',) if payload.startswith((b'\xff\xfe', b'\xfe\xff')) else ('utf-8-sig', 'cp932')
        for encoding in encodings:
            try:
                contents = payload.decode(encoding)
                break
            except UnicodeError:
                continue
        else:
            raise ValueError('CSVはUTF-8・Shift-JIS・UTF-16で保存してください。')
        options = {}
        try:
            options['dialect'] = csv.Sniffer().sniff(contents[:8192], delimiters=',\t;')
        except csv.Error:
            pass
        records = csv.reader(io.StringIO(contents), **options)
        header = {}
        for i, name in enumerate(next(records, [])):
            header.setdefault(name.strip(), i)
        required = ('DAC_A_nominal_V', 'Vout_A_nominal_V')
        if any(name not in header for name in required):
            raise ValueError('CSVにDAC_A_nominal_VとVout_A_nominal_Vの列が必要です。')
        indices = [header[name] for name in required]
        for record in records:
            if not any(value.strip() for value in record):
                continue
            row = len(points) + 1
            try:
                dac, vout = (float(record[index]) for index in indices)
            except (ValueError, IndexError) as error:
                raise ValueError(f'{row}行のDAC_A・Vout_Aには数値が必要です。') from error
            # 任意列は列名で取得する。抵抗列がないデータも従来どおり読める。
            optional = [record[header[name]] if name in header and header[name] < len(record) else None
                        for name in ('resistance_A_ohm', 'sweep')]
            append(row, dac, vout, *optional)
    else:
        raise ValueError('入力はLP512形式の.csvにしてください。')

    # 配列化は一度だけ行う。先頭3列が解析行・Vout・Ipol、後ろ2列が抵抗・掃引名。
    arrays = tuple(np.array([point[i] for point in points], dtype=dtype)
                   for i, dtype in enumerate((int, float, float, float, str)))
    if not validate_selection:
        return arrays if metadata else arrays[:3]
    pair = config.get('analysis_rows')
    if (not isinstance(pair, (list, tuple)) or len(pair) != 2
            or any(type(value) is not int for value in pair) or pair[0] >= pair[1]):
        raise ValueError('analysis_rowsは開始行・終了行の整数で指定してください。')
    rows, x, y, resistance, _ = arrays
    mask = row_mask(rows, pair)
    if rows[mask].tolist() != list(range(pair[0], pair[1] + 1)):
        raise ValueError('解析範囲に生データの存在しない行があります。')
    # 手順5–7: Ipol*R+Voutを指定上下限と比較する。Voutそのものの上下限ではない。
    total = (y * resistance + x)[mask]
    lower, upper = applicability_bounds(config)
    if not np.all((lower < total) & (total < upper)):
        raise ValueError(f'解析範囲に適用判定（{lower:g} < Vin+Vout < {upper:g}）の外側の点があります。')
    data = arrays if include_all else tuple(array[mask] for array in arrays)
    return data if metadata else data[:3]


def select_applicable_rows(source, config):
    """上下限変更時、同じ掃引内の連続した適用点へ解析範囲を更新する。"""
    rows, x, y, resistance, sweeps = read_points(source, config, metadata=True, validate_selection=False)
    lower, upper = applicability_bounds(config)
    total = y * resistance + x
    valid = (lower < total) & (total < upper)
    runs = []
    for i in np.flatnonzero(valid):
        if runs and rows[i] == rows[runs[-1][-1]] + 1 and sweeps[i] == sweeps[runs[-1][-1]]:
            runs[-1].append(i)
        else:
            runs.append([i])
    runs = [run for run in runs if len(run) >= 2]
    if not runs:
        raise ValueError('指定した適用範囲に連続する測定点が2点以上ありません。')
    first, last = config.get('analysis_rows', [0, 0])
    def rank(run):
        overlap = np.count_nonzero((rows[run] >= first) & (rows[run] <= last))
        return overlap, sweeps[run[0]].casefold() == 'forward', len(run)
    run = max(runs, key=rank)
    return [int(rows[run[0]]), int(rows[run[-1]])]


# 数値計算（手順8–24）


def row_mask(rows, pair):
    """開始行・終了行をともに含む選択。測定点の並びは変えない。"""
    return (rows >= pair[0]) & (rows <= pair[1])


def fit_line(x, y, rows) -> Fit:
    """切片付き最小二乗法で y=a*x+b を求める。係数は固定しない。"""
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    if len(x) < 2 or not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)):
        raise ValueError(f"近似範囲{rows}: 有限な点が2点以上必要です。")

    # a = Σ[(x-x平均)(y-y平均)] / Σ[(x-x平均)^2]、b = y平均-a*x平均。
    # @ は内積。途中の傾き・切片は丸めず、以降の計算にそのまま渡す。
    dx = x - x.mean()
    denominator = float(dx @ dx)
    if denominator == 0:
        raise ValueError(f"近似範囲{rows}: 電圧がすべて同一です。")
    slope = float(dx @ (y - y.mean()) / denominator)
    intercept = float(y.mean() - slope * x.mean())
    residual = float(np.sum((y - (slope * x + intercept)) ** 2))
    total = float(np.sum((y - y.mean()) ** 2))
    return Fit(slope, intercept, 1 - residual / total if total else None,
               tuple(rows), len(x))


def ion_region(voltage):
    """表示中の適用範囲で、最小電圧から0 V未満までをすべて選ぶ。"""
    x = np.asarray(voltage, dtype=float)
    mask = np.isfinite(x) & (x < 0)
    if np.count_nonzero(mask) < 2:
        raise ValueError('イオン近似には、適用範囲内でVoutが0 V未満の測定点が2点以上必要です。')
    return mask


def vp0_interval_mask(voltage):
    """Vp0候補は両端がVout>0かつ電圧差が0でない区間。末尾に区間はない。"""
    x = np.asarray(voltage, dtype=float)
    return np.append((x[:-1] > 0) & (x[1:] > 0) & (np.diff(x) != 0), False)


def correct_trace(x, y, rows) -> Trace:
    """負電圧のイオン近似 → 差し引き → ln変換 → Vp0用の差分。"""
    # 手順8–10: 適用範囲の負電圧点を全て使い、Iion=a*Vout+bを計算。
    # maskは選ぶ点を示すTrue/Falseの配列、x[mask]は選ばれた電圧だけの配列。
    mask = ion_region(x)
    selected = rows[mask]
    ion_fit = fit_line(x[mask], y[mask], (int(selected[0]), int(selected[-1])))
    a, b = ion_fit.slope, ion_fit.intercept
    ion = a * x + b
    electron = y - ion

    # 2点を通る直線の差し引きでは、本来0の残差が10^-23 A程度になることがある。
    # ln≈-50という丸め誤差を防ぐため、差し引く値の大きさと計算精度で0を判定する。
    # 固定の電流閾値やlnの下限は設けず、実際に小さい電流は保持する。
    tolerance = ROUNDOFF_FACTOR * (np.abs(y) + np.abs(a * x) + abs(b))
    electron = np.where(np.abs(electron) <= tolerance, 0., electron)
    if not np.all(np.isfinite(electron)):
        raise ValueError("補正電流に非有限値があります。")

    # 手順11–13: A単位の補正電流の絶対値を自然対数に変換する。
    # Ie=0のlnは未定義なのでNaNにし、グラフ・近似・Vf候補から除く。
    with np.errstate(divide="ignore"):
        logarithm = np.log(np.abs(electron))
    logarithm[~np.isfinite(logarithm)] = np.nan
    if not np.any(np.isfinite(logarithm)):
        raise ValueError("ln表示可能な補正電流がありません。")

    # 手順15: Vp0には適用範囲のln変換前の補正Ieを使う。
    # 隣接する両端が正電圧の区間だけで最大を探し、その先頭点を採用する。
    # 差分そのものは負電圧点もCSVに残す。末尾は次の点がないためNaN。
    delta_x = np.diff(x)
    # 同じ電圧の2点では微分が未定義。点を削除・結合せず、その区間だけNaNにする。
    intervals = np.full(len(delta_x), np.nan)
    np.divide(np.abs(np.diff(electron)), np.abs(delta_x), out=intervals, where=delta_x != 0)
    derivative = np.append(intervals, np.nan)
    candidates = np.flatnonzero(vp0_interval_mask(x) & np.isfinite(derivative))
    if not len(candidates):
        raise ValueError('Vp0の決定には、隣接する両端がVout > 0かつ電圧差が0でない有効な測定区間が必要です。')
    peak = int(candidates[np.argmax(derivative[candidates])])
    return Trace(ion, electron, logarithm, derivative, ion_fit, peak)


def minimum_floating_potential(voltage, log_current, data_rows, *, average_two=False, positive_only=False):
    """目視で方法を選んだ後、実測最小点またはln値が小さい2点からVfを計算。"""
    # ln値の昇順で選び、同率なら元の解析行が小さい方を優先する。
    points = [(float(y), int(r), float(x)) for x, y, r in zip(voltage, log_current, data_rows)
              if math.isfinite(float(x)) and math.isfinite(float(y)) and (not positive_only or float(x) > 0)]
    points.sort(key=lambda point: (point[0], point[1]))
    count = 2 if average_two else 1
    if len(points) < count:
        raise ValueError('Vfを決定する有限なln表示の測定点が不足しています。'+(' Vout > 0 Vの点が必要です。' if positive_only else ''))
    chosen = points[:count]
    return FloatingPotential(sum(p[2] for p in chosen) / count,
                             tuple(p[1] for p in chosen), tuple(p[2] for p in chosen),
                             points[0][1], points[0][2], points[0][0],
                             'two_lowest_log_voltage_midpoint' if average_two else 'measured_log_minimum')


def check_region_bounds(voltage, rows, pair, *, label, lower, upper=None):
    x, rows = np.asarray(voltage), np.asarray(rows)
    mask = row_mask(rows, pair)
    valid = x[mask] >= lower
    if upper is not None:
        valid &= x[mask] < upper
    if not np.all(valid):
        bounds = f'{lower:.6g} V以上' + (f'、{upper:.6g} V未満' if upper is not None else '')
        raise ValueError(f'{label}は{bounds}の点から選択してください。')


def fit_log_region(x, rows, trace, pair, key, vf):
    """GUI・ターミナルで共通の領域検証とln直線近似。係数は測定点から求める。"""
    label = '電子減速領域' if key == 'retarding' else '電子飽和領域'
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        raise ValueError(f'{label}の開始・終了行を指定してください。')
    if not rows[0] <= pair[0] < pair[1] <= rows[-1]:
        raise ValueError(f'{label}は解析範囲内で2点以上選択してください。')
    mask = row_mask(rows, pair)
    if np.any(x[mask] <= 0):
        raise ValueError(f'{label}はVout > 0 Vの測定点から選んでください。')
    if key == 'retarding':
        check_region_bounds(x, rows, pair, label=label, lower=vf, upper=x[trace.peak_index])
    mask &= np.isfinite(trace.log_current)
    return fit_line(x[mask], trace.log_current[mask], pair)


def intersect_fits(retarding: Fit, saturation: Fit) -> float:
    """減速直線 ar*V+br と飽和直線 as*V+bs の交点の電圧Vp。"""
    denominator = retarding.slope - saturation.slope
    if math.isclose(denominator, 0, abs_tol=1e-14):
        raise ValueError("減速領域と飽和領域の直線が平行で、Vpを決定できません。")
    vp = (saturation.intercept - retarding.intercept) / denominator
    if not math.isfinite(vp):
        raise ValueError("交点のVpが有限値になりません。")
    return vp


def vp_bracketing_rows(voltage, logarithm, rows, vp):
    """Vpの両側にある最も近い有限な測定点で局所直線を作る。"""
    x, y, rows = np.asarray(voltage), np.asarray(logarithm), np.asarray(rows)
    valid = np.isfinite(y) & np.isfinite(x)
    below = np.flatnonzero(valid & (x < vp))
    above = np.flatnonzero(valid & (x > vp))
    if not len(below) or not len(above):
        raise ValueError('Vpの両側に測定点がありません。Iesを外挿せず、近似領域を見直してください。')
    left = int(below[np.argmax(x[below])])
    right = int(above[np.argmin(x[above])])
    # Vpと一致する測定点がある場合は、その点も含める。
    exact = np.flatnonzero(valid & (x == vp)).tolist()
    indices = sorted({left, right, *exact})
    skipped = set(range(min(indices), max(indices) + 1)) - set(indices)
    if any(valid[i] for i in skipped):
        raise ValueError('Vp近傍の行が連続していません。単一の掃引範囲を選択してください。')
    return [int(rows[min(indices)]), int(rows[max(indices)])]


def fit_vp_neighbors(voltage, logarithm, rows, vp) -> Fit:
    """正電圧の有効点からVp直前・直後を自動選択し、ln値を局所近似。"""
    x, y, rows = map(np.asarray, (voltage, logarithm, rows))
    valid = (x > 0) & np.isfinite(x) & np.isfinite(y)
    lo, hi = vp_bracketing_rows(x, np.where(valid, y, np.nan), rows, vp)
    mask = (rows >= lo) & (rows <= hi) & valid
    # Vpと同じ電圧の点があれば、それも含む。両側に点がなければ外挿しない。
    return fit_line(x[mask], y[mask], (lo, hi))


def calculate_parameters(x, rows, trace: Trace, config):
    """手動選択した減速・飽和領域からVp・Ies・Te・電子密度を計算。"""
    parameters = {key: float(config.get(key, value))
                  for key, value in DEFAULT_PARAMETERS.items()}
    for key, value in parameters.items():
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"設定 {key} は正の有限値にしてください。")

    # 手順14: Vfは正電圧のln最小点、またはln値が小さい2点の電圧平均。
    # 明瞭な最小点かどうかは利用者がグラフを見て方法を選ぶ。
    mode = config.get("floating_potential_mode")
    if mode not in ("minimum", "lowest_two"):
        raise ValueError("Vfを目視で確認し、最小実測点か小さい2点の平均を選んでください。")
    floating = minimum_floating_potential(
        x, trace.log_current, rows, average_two=mode == "lowest_two", positive_only=True)

    # 手順16–19: ドラッグして保存した行範囲を使ってln(Ie)を近似する。
    # 減速領域はVf以上Vp0未満。飽和領域にはVp0による範囲制限を設けない。
    fits = {"ion": trace.ion_fit}
    for key in ('retarding', 'saturation'):
        fits[key] = fit_log_region(x, rows, trace, config.get(f'{key}_fit_rows'), key,
                                   floating.voltage_V)

    # 手順20–22: 2直線の交点をVpとし、その前後の点の近似は自動で求める。
    # 局所直線へのVp代入値はln(Ies)。expで電流[A]に戻してから密度に使う。
    retarding = fits["retarding"]
    vp = intersect_fits(retarding, fits["saturation"])
    fits["vp"] = fit_vp_neighbors(x, trace.log_current, rows, vp)
    ln_ies = fits["vp"].at(vp)
    ies = math.exp(ln_ies)

    # 手順18: ln(Ie)の減速領域の傾きからTe[K]=e/(k*a_ret)を求める。
    # eは電気素量[C]。Te[eV]は1/a_ret。傾きが0以下なら計算を止める。
    if retarding.slope <= 0:
        raise ValueError("電子減速領域の傾きが0以下のため、正の電子温度を計算できません。")
    charge = parameters["electron_charge_C"]
    k = parameters["boltzmann_J_per_K"]
    mass = parameters["electron_mass_kg"]
    temperature = charge / k / retarding.slope

    # 手順23–24: 球面積S=4*pi*r^2、平均速度v=sqrt(8*k*Te/(pi*me))。
    # ne=4*Ies/(e*S*v)はm^-3。10^6で割るとcm^-3になる。
    area = 4 * math.pi * parameters["probe_radius_m"] ** 2
    speed = math.sqrt(8 * k * temperature / (math.pi * mass))
    density = 4 * ies / (charge * area * speed)
    metrics = {
        "probe_area_m2": area,
        "electron_temperature_K": temperature,
        "electron_temperature_eV": 1 / retarding.slope,
        "electron_mean_speed_m_per_s": speed,
        "plasma_potential_V": vp,
        "log_electron_saturation_current": ln_ies,
        "electron_saturation_current_A": ies,
        "electron_density_m3": density,
        "electron_density_cm3": density / 1e6,
        "density_log10_cm3": math.log10(density / 1e6),
        "derivative_peak_data_row": int(rows[trace.peak_index]),
        "derivative_peak_voltage_V": float(x[trace.peak_index]),
        "floating_potential_V": floating.voltage_V,
        "minimum_log_voltage_V": floating.minimum_voltage_V,
    }
    if not all(math.isfinite(value) for value in metrics.values()):
        raise ValueError("結果に非有限値が生じました。測定値と近似範囲を確認してください。")
    return fits, metrics, floating


def calculate(source, config) -> Calculation:
    """生データから補正・ln・Vp・Te・密度までをまとめて計算する公開関数。"""
    config = normalize_config(config)
    data = read_points(source, config, include_all=True, metadata=True)
    all_points, resistance = data[:3], data[3]
    all_rows = all_points[0]
    mask = row_mask(all_rows, config['analysis_rows'])
    rows, x, y = (array[mask] for array in all_points)
    trace = correct_trace(x, y, rows)
    fits, metrics, floating = calculate_parameters(x, rows, trace, config)
    return Calculation(rows, x, y, trace, fits, metrics, floating, all_points,
                       resistance, applicability_bounds(config))


def read_trace(source, config, *, details=False):
    """Vf・近似領域が未設定でも、GUIでイオン補正とlnの点を表示する。"""
    rows, x, y = read_points(source, config)
    trace = correct_trace(x, y, rows)
    return (rows, x, trace) if details else (rows, x, trace.log_current)


# 結果の保存・グラフ（GUIと計算専用版で共用）


def output_directory(source, config, config_path, *, root=None):
    """データ固有の出力先。保存済みの指定があればGUIとターミナルで共有する。"""
    if config_path is None:
        return Path(root or ROOT).resolve() / 'outputs' / Path(source).stem
    path = Path(config_path).resolve()
    if config.get('output_dir'):
        return (path.parent / config['output_dir']).resolve()
    name = path.stem if path.parent.name == 'profiles' else Path(source).stem
    return Path(root or ROOT).resolve() / 'outputs' / name


def export_arrays(result):
    """GUI・計算専用版の結果を、共通のCSV・グラフ処理へ渡す。"""
    return result.all_points or (result.rows, result.voltage, result.current), result.trace


def export_resistance(result, size):
    """全測定点と同じ並びの抵抗。計算・CSV・グラフで共有する。"""
    values = getattr(result, 'all_resistance', None)
    return np.full(size, RESISTANCE_OHM) if values is None else np.asarray(values, dtype=float)


# CSVの日本語名・単位の対応表。数値計算の処理とは分けて管理する。
RESULT_FIELDS = {
    'floating_potential_V': ('浮遊電位 Vf', 'V'),
    'derivative_peak_voltage_V': ('仮プラズマ空間電位 Vp0', 'V'),
    'plasma_potential_V': ('プラズマ空間電位 Vp', 'V'),
    'electron_saturation_current_A': ('電子飽和電流 Ies', 'A'),
    'electron_density_cm3': ('電子密度 ne（cm^-3）', 'cm^-3'),
    'electron_density_m3': ('電子密度 ne（m^-3）', 'm^-3'),
    'electron_temperature_K': ('電子温度 Te（K）', 'K'),
    'electron_temperature_eV': ('電子温度 Te（eV）', 'eV'),
    'electron_mean_speed_m_per_s': ('電子の熱運動平均速度', 'm/s'),
    'probe_area_m2': ('球状プローブの表面積', 'm^2'),
    'probe_radius_m': ('球状プローブの半径', 'm'),
    'electron_mass_kg': ('電子質量', 'kg'),
    'boltzmann_J_per_K': ('ボルツマン定数', 'J/K'),
    'electron_charge_C': ('電気素量', 'C'),
    'resistance_ohm': ('使用したA側抵抗（測定データ優先）', 'ohm'),
    'resistance_A_ohm': ('測定点のA側抵抗', 'ohm'),
    'applicability_min_V': ('適用範囲の下限（Vin＋Vout）', 'V'),
    'applicability_max_V': ('適用範囲の上限（Vin＋Vout）', 'V'),
    'analysis_rows': ('解析する掃引の行範囲', ''),
    'floating_potential_mode': ('Vfの決定方法', ''),
    'source': ('入力ファイル', ''),
    'log_electron_saturation_current': ('電子飽和電流の自然対数 ln(Ies / 1 A)', ''),
    'density_log10_cm3': ('電子密度の常用対数 log10(ne / 1 cm^-3)', ''),
    'derivative_peak_data_row': ('Vp0に採用したCSVデータ行', ''),
    'minimum_log_voltage_V': ('ln最小測定点の電圧', 'V'),
    'maximum_abs_dIe_dV': ('正電圧範囲の隣接差分 ΔIe/ΔVout の最大値（ln変換前）', ''),
    'vp0_candidate_interval': ('Vp0候補の区間判定（1:隣接両点Vout>0・電圧差≠0）', ''),
    'DAC_A_nominal_V': ('入力 DAC_A_nominal_V', ''),
    'Vout_A_nominal_V': ('入力 Vout_A_nominal_V', ''),
    'Vout': ('変換後の電圧 Vout', ''),
    'Ipol': ('変換後の電流 Ipol', ''),
    'Vin_plus_Vout': ('Vin＋Vout', ''),
    'applicable': ('適用範囲の判定（1:範囲内）', ''),
    'selected_sweep': ('解析する掃引の判定（1:選択点）', ''),
    'ion_baseline': ('差し引くイオン近似電流', ''),
    'Ie': ('イオン差し引き後の電子電流 Ie', ''),
    'ln_abs_Ie': ('補正電流の自然対数 ln(|Ie| / 1 A)', ''),
    'abs_delta_Ie': ('隣接する補正電流の差 ΔIe', ''),
    'abs_delta_Vout': ('隣接する電圧の差 ΔVout', ''),
    'abs_dIe_dV': ('隣接差分 ΔIe/ΔVout（ln変換前）', ''),
    'voltage_V': ('採用した浮遊電位 Vf', ''),
    'rows': ('Vfに使った測定点の行', ''),
    'endpoint_voltages_V': ('Vfに使った測定点の電圧', ''),
    'minimum_row': ('ln最小測定点の行', ''),
    'minimum_voltage_V': ('ln最小測定点の電圧', ''),
    'minimum_log_current': ('ln最小値', ''),
    'method': ('Vfの決定方法', ''),
}


FIT_LABELS = {'ion': 'イオン', 'retarding': '電子減速領域', 'saturation': '電子飽和領域', 'vp': 'Vp前後の局所近似'}


FIT_FIELDS = {'slope': '傾き', 'intercept': '切片', 'r_squared': '決定係数 R²', 'rows': '使用行範囲', 'points': '使用点数'}


def result_label(category, key):
    """CSVの日本語名。項目名と単位は対応表から一意に決める。"""
    if category == 'fit_retarding' and key == 'slope':
        return '電子減速領域の傾き ΔIpol/ΔVout（ln表示）'
    if category.startswith('fit_'):
        return f'{FIT_LABELS[category[4:]]}：{FIT_FIELDS.get(key, key)}'
    for name, label in FIT_LABELS.items():
        if key == f'{name}_fit_value':
            return f'{label}の近似値（ln表示）'
        if key == f'in_{name}_fit':
            return f'{label}の選択点判定（1:選択点）'
    return RESULT_FIELDS.get(key, (key, ''))[0]


def csv_value(value):
    """CSV用の値。未定義値は空欄、行範囲などはJSON文字列で記録する。"""
    if isinstance(value, (tuple, list, dict)):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    if isinstance(value, (float, np.floating)) and not np.isfinite(value):
        return ''
    # 表計算ソフト等で数式として解釈される文字列は、文字列として保存する。
    if isinstance(value, str) and value.startswith(('=', '+', '-', '@')):
        return "'" + value
    return value


def write_csv_table(path, header, records):
    """日本語に対応したUTF-8 BOM付きCSV。全出力で欠損値の扱いをそろえる。"""
    with Path(path).open('w', newline='', encoding='utf-8-sig') as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows([csv_value(value) for value in record] for record in records)


def write_point_csv(path, rows, columns, *, row_label='data_row'):
    """測定点ごとの横持ちCSV。columnsは列名→計算配列の順序付き辞書。"""
    records = ([int(row), *(values[i] for values in columns.values())] for i, row in enumerate(rows))
    write_csv_table(path, [row_label, *columns], records)


def write_calculation_csvs(out, result, source, config):
    """全結果を縦持ちCSVへ集約する。未定義のln・差分は空欄で保存する。"""
    out = Path(out)
    raw, trace = export_arrays(result)
    # 最初の6行だけで主要結果を確認できるようにし、詳細・計算途中は後ろへ置く。
    main_metrics = ('floating_potential_V', 'derivative_peak_voltage_V', 'plasma_potential_V',
                    'electron_saturation_current_A', 'electron_density_cm3')
    records = [('metrics', '', key, result.metrics[key], RESULT_FIELDS[key][1]) for key in main_metrics[:2]]
    records.append(('fit_retarding', '', 'slope', result.fits['retarding'].slope, '1/V'))
    records.extend(('metrics', '', key, result.metrics[key], RESULT_FIELDS[key][1]) for key in main_metrics[2:])
    # 減速直線の傾き[1/V]と、ln変換前の隣接差分[A/V]を区別して記録する。
    records.append(('analysis_summary', '', 'maximum_abs_dIe_dV', float(trace.derivative[trace.peak_index]), 'A/V'))
    records.append(('input', '', 'source', str(source), ''))
    settings = normalize_config(config)
    actual_resistance = np.unique(export_resistance(result, len(raw[0]))).tolist()
    settings['resistance_ohm'] = actual_resistance[0] if len(actual_resistance) == 1 else actual_resistance
    for key in (*DEFAULT_PARAMETERS, 'resistance_ohm', 'applicability_min_V', 'applicability_max_V', 'analysis_rows', 'floating_potential_mode'):
        records.append(('settings', '', key, settings.get(key), RESULT_FIELDS.get(key, ('', ''))[1]))
    for key, value in result.metrics.items():
        if key not in main_metrics:
            records.append(('metrics', '', key, value, RESULT_FIELDS.get(key, ('', ''))[1]))
    for name, fit in result.fits.items():
        for key, value in asdict(fit).items():
            if name == 'retarding' and key == 'slope':
                continue
            unit = {'slope': 'A/V' if name == 'ion' else '1/V',
                    'intercept': 'A' if name == 'ion' else ''}.get(key, '')
            records.append((f'fit_{name}', '', key, value, unit))
    for key, value in asdict(result.floating_potential).items():
        records.append(('floating_potential', '', key, value,
                        'V' if key.endswith(('_V', 'voltages_V')) else ''))
    def write(path, entries):
        # 先頭3列は人向け。後半の英語列はPython等から読み込むために保持する。
        records = ([result_label(category, key), value, unit, category, row, key, value, unit]
                   for category, row, key, value, unit in entries)
        write_csv_table(path, ['項目（日本語）', '値', '単位', 'category', 'data_row', 'item', 'value', 'unit'], records)

    write(out / 'results_summary.csv', records)
    rows, x, y = raw
    resistance = export_resistance(result, len(rows))
    total = y * resistance + x
    lower, upper = applicability_bounds(config)
    applicable = (lower < total) & (total < upper)
    selected = np.isin(rows, result.rows)
    raw_columns = [('DAC_A_nominal_V', -y * resistance, 'V'),
                   ('resistance_A_ohm', resistance, 'ohm'),
                   ('Vout_A_nominal_V', -x, 'V'), ('Vout', x, 'V'), ('Ipol', y, 'A'),
                   ('Vin_plus_Vout', total, 'V'), ('applicable', applicable.astype(int), ''),
                   ('selected_sweep', selected.astype(int), '')]
    columns = [('ion_baseline', trace.ion_current, 'A'),
               ('Ie', trace.electron_current, 'A'), ('ln_abs_Ie', trace.log_current, ''),
               ('abs_delta_Ie', np.append(np.abs(np.diff(trace.electron_current)), np.nan), 'A'),
               ('abs_delta_Vout', np.append(np.abs(np.diff(result.voltage)), np.nan), 'V'),
               ('abs_dIe_dV', trace.derivative, 'A/V'),
               ('vp0_candidate_interval', vp0_interval_mask(result.voltage).astype(int), '')]
    for name, fit in result.fits.items():
        if name == 'ion':
            continue
        columns.extend([(f'{name}_fit_value', fit.at(result.voltage), ''),
                        (f'in_{name}_fit', row_mask(result.rows, fit.rows).astype(int), '')])
    # 生データと解析点を同じ形式で記録し、計算過程を行番号から追跡できるようにする。
    for category, point_rows, point_columns in [('raw', rows, raw_columns), ('analysis', result.rows, columns)]:
        for i, row in enumerate(point_rows):
            records.extend((category, int(row), key, values[i].item(), unit)
                           for key, values, unit in point_columns)
    write(out / 'calculation_results.csv', records)


def draw_ion_fit(ax, voltage, current, fit):
    """グラフ3は負電圧の近似使用点も表示し、直線を正電圧側まで延長する。"""
    x, y = np.asarray(voltage), np.asarray(current)
    finite = np.isfinite(x) & np.isfinite(y)
    negative = finite & (x < 0)
    ax.plot(x[finite], y[finite], 'o', linestyle='None', ms=3, color='#526170', label='Ipol')
    ax.plot(x[negative], y[negative], 'o', linestyle='None', ms=5, color='#ce721b',
            label='Negative-voltage points used for ion fit')
    xx = np.linspace(float(x[finite].min()), float(x[finite].max()), 200)
    ax.plot(xx, fit.at(xx), lw=2.2, color='#246caf',
            label=f'Iion={fit.slope:.6g}V {fit.intercept:+.6g}')
    ax.axvline(0, color='gray', lw=.8, ls=':')


def plot_stages(out, result):
    """全解析段階を個別PNGと一覧図に保存する。画面・Tkは起動しない。"""
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    out = Path(out)
    graphs = out / 'graphs'
    graphs.mkdir(parents=True, exist_ok=True)
    (raw_rows, raw_x, raw_y), trace = export_arrays(result)
    resistance = export_resistance(result, len(raw_rows))
    lower, upper = getattr(result, 'applicability_bounds', (-10., 10.))
    visible = result.voltage > 0
    x, log = result.voltage[visible], trace.log_current[visible]
    rows = result.rows[visible]
    vp = result.metrics['plasma_potential_V']
    stages = [
        ('01_raw_iv', 'Raw I-V'), ('02_applicable_iv', 'Applicable I-V'),
        ('03_ion_fit', 'Ion fit from negative voltage'),
        ('04_corrected_current', 'Ion-subtracted current'),
        ('05_log_current', 'Log current and Vf'), ('06_derivative', 'Adjacent difference and Vp0'),
        ('07_retarding_fit', 'Electron retarding fit'), ('08_saturation_fit', 'Electron saturation fit'),
        ('09_vp_intersection', 'Intersection of retarding and saturation fits'),
        ('10_vp_neighbors', 'Local fit around Vp and Ies'),
    ]
    colors = {'retarding': '#246caf', 'saturation': '#ce721b', 'vp': '#398147'}

    def points(ax, xx, yy, **kwargs):
        mask = np.isfinite(xx) & np.isfinite(yy) & (xx > 0)
        ax.plot(xx[mask], yy[mask], 'o', linestyle='None', ms=3, **kwargs)

    def fit_line_on(ax, key, *, extend=False):
        fit = result.fits[key]
        mask = row_mask(rows, fit.rows) & np.isfinite(log)
        if not mask.any():
            return
        points(ax, x[mask], log[mask], color=colors[key], label=f'{key} points')
        left, right = float(x[mask].min()), float(x[mask].max())
        if extend:
            left, right = min(left, vp), max(right, vp)
        xx = np.linspace(max(left, np.nextafter(0., 1.)), right, 100)
        ax.plot(xx, fit.at(xx), color=colors[key], lw=2.2,
                label=f'{key}: y={fit.slope:.6g}x {fit.intercept:+.6g}')

    def draw(ax, index):
        if index in (0, 1):
            mask = raw_x > 0
            if index == 1:
                total = raw_y * resistance + raw_x
                mask &= (lower < total) & (total < upper)
            points(ax, raw_x[mask], raw_y[mask], color='#526170', label='Ipol')
        elif index == 2:
            draw_ion_fit(ax, result.voltage, result.current, result.fits['ion'])
        elif index == 3:
            points(ax, x, trace.electron_current[visible], color='#526170', label='Ie')
            ax.axhline(0, color='gray', lw=.7)
        elif index == 5:
            eligible = vp0_interval_mask(result.voltage)[visible]
            points(ax, x[eligible], trace.derivative[visible][eligible], color='#526170', label='abs(delta Ie/delta V), both Vout > 0')
            peak = result.metrics['derivative_peak_voltage_V']
            if peak > 0:
                ax.axvline(peak, color='gray', ls='--', label=f'Vp0={peak:.6g} V')
        else:
            points(ax, x, log, color='#526170', label='ln(|Ie| / 1 A)')
            if index == 4:
                vf = result.metrics['floating_potential_V']
                ax.axvline(vf, color='#943a94', ls=':', label=f'Vf={vf:.6g} V')
            for key in {6: ('retarding',), 7: ('saturation',),
                        8: ('retarding', 'saturation'), 9: ('vp',)}.get(index, ()):
                fit_line_on(ax, key, extend=index == 8)
            if index in (8, 9):
                value = result.fits['retarding'].at(vp) if index == 8 else result.metrics['log_electron_saturation_current']
                label = f'Vp={vp:.6g} V'
                if index == 9:
                    label += f", Ies={result.metrics['electron_saturation_current_A']:.6g} A"
                    fit = result.fits['vp']
                    near = x[row_mask(rows, fit.rows)]
                    margin = max(float(np.ptp(near)) * .5, abs(vp) * .01, 1e-9)
                    left, right = max(0, float(near.min()) - margin), float(near.max()) + margin
                    ax.set_xlim(left, right)
                    # 局所近似は縦軸もVp付近に絞り、近似直線の傾きを確認できるようにする。
                    local = log[(x >= left) & (x <= right) & np.isfinite(log)]
                    limits = np.append(local, value)
                    padding = max(float(np.ptp(limits)) * .15, .01)
                    ax.set_ylim(float(limits.min()) - padding, float(limits.max()) + padding)
                ax.scatter(vp, value, marker='*', s=100, color='black', label=label, zorder=6)
        # lnの中身は1 Aで割った無次元量。電流[A]のグラフと区別して表示する。
        ylabel = ('ln(|Ie| / 1 A)' if index in (4, 6, 7, 8, 9)
                  else 'abs(delta Ie/delta V) (A/V)' if index == 5 else 'Ipol (A)')
        ax.set(title=stages[index][1], xlabel='Vout (V)', ylabel=ylabel)
        if index not in (2, 9):
            ax.set_xlim(left=0)
        ax.grid(alpha=.2)
        ax.legend(fontsize=8)

    for index, (name, _) in enumerate(stages):
        figure = Figure(figsize=(10, 6), layout='constrained')
        FigureCanvasAgg(figure)
        draw(figure.subplots(), index)
        figure.savefig(graphs / f'{name}.png', dpi=160)
        figure.clear()
    overview = Figure(figsize=(16, 9), layout='constrained')
    FigureCanvasAgg(overview)
    m = result.metrics
    overview.suptitle(
        f"Vf={m['floating_potential_V']:.6g} V, Vp0={m['derivative_peak_voltage_V']:.6g} V, Vp={vp:.6g} V\n"
        f"Te={m['electron_temperature_K']:.6g} K ({m['electron_temperature_eV']:.6g} eV), "
        f"Ies={m['electron_saturation_current_A']:.6g} A, ne={m['electron_density_cm3']:.6g} cm^-3")
    for ax, index in zip(overview.subplots(2, 3).flat, (0, 2, 3, 8, 5, 9)):
        draw(ax, index)
    overview.savefig(out / 'analysis.png', dpi=160)
    overview.clear()


def save_results(out, result, source, config, config_path):
    """単独版も全計算結果CSVと全解析段階の画像を保存する。"""
    out = Path(out).resolve()
    summary, table = out / 'summary.json', out / 'analysis.csv'
    targets = {summary, table, *(out / name for name in
               ('results_summary.csv', 'calculation_results.csv', 'iv_all.csv', 'analysis.png'))}
    protected = {Path(source).resolve()}
    if config_path is not None:
        protected.add(Path(config_path).resolve())
    if targets & protected:
        raise ValueError('出力先が入力・設定ファイルと重なっています。別フォルダを指定してください。')
    payload = {'input': str(source), 'config': config, 'metrics': result.metrics,
               'fits': {key: asdict(fit) for key, fit in result.fits.items()},
               'floating_potential': result.floating_potential.to_dict()}
    # 依存ライブラリ不足なら書き出し前に止め、--no-exportの計算のみは維持する。
    import matplotlib
    out.mkdir(parents=True, exist_ok=True)
    summary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    write_point_csv(table, result.rows, {
        'voltage_V': result.voltage, 'Ipol_A': result.current,
        'ion_baseline_A': result.trace.ion_current, 'Ie_A': result.trace.electron_current,
        'ln_abs_Ie': result.trace.log_current, 'abs_dIe_dV': result.trace.derivative})
    # 全測定点の変換と適用判定も、1点1行のCSVで確認できるようにする。
    (rows, x, y), _ = export_arrays(result)
    resistance = export_resistance(result, len(rows))
    total = y * resistance + x
    lower, upper = applicability_bounds(config)
    write_point_csv(out / 'iv_all.csv', rows, {
        'voltage_V': x, 'Ipol_A': y, 'resistance_A_ohm': resistance,
        'Vin_plus_Vout_V': total, 'applicable': ((lower < total) & (total < upper)).astype(int),
        'selected_sweep': np.isin(rows, result.rows).astype(int)})
    write_calculation_csvs(out, result, source, config)
    plot_stages(out, result)


# ターミナルからの実行


def config_ready(config):
    """Vf方法と2領域を利用者が保存済みかを確認する。"""
    return (config.get('floating_potential_mode') in ('minimum', 'lowest_two')
            and bool(config.get('retarding_fit_rows')) and bool(config.get('saturation_fit_rows')))


def available_configs(*, root=None):
    """入力が存在する設定だけを集める。設定・測定ファイルは変更しない。"""
    root = Path(root or ROOT)
    candidates = [*root.joinpath('profiles').glob('*.json'),
                  *(root / name for name in ('settings.json', 'analysis_config.json', 'earth_analysis_config.json'))]
    available = []
    for path in sorted(candidates, key=lambda path: str(path).casefold()):
        try:
            config = load_config(path)
            if (path.parent / config['input']).is_file():
                available.append((path.resolve(), config))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return available


def choose_config(explicit=None, *, root=None):
    """自動実行向け。指定がなければ保存済みの最新設定を使う。"""
    if explicit is not None:
        return Path(explicit).resolve()
    ready = [path for path, config in available_configs(root=root) if config_ready(config)]
    if not ready:
        raise ValueError('計算用の保存済み設定がありません。--configで設定JSON、または--inputと解析条件を直接指定してください。GUIは不要です。')
    return max(ready, key=lambda path: path.stat().st_mtime_ns).resolve()


def select_config(*, root=None):
    """ターミナルで番号を選ぶ。入力ごとに保存された範囲を使い、流用しない。"""
    root = Path(root or ROOT).resolve()
    available = available_configs(root=root)
    if not available:
        raise ValueError('入力データの設定がありません。--configで設定JSON、または--inputと解析条件を直接指定してください。')
    print('解析する入力データを選んでください。')
    for number, (path, config) in enumerate(available, 1):
        source = (path.parent / config['input']).resolve()
        display = source.relative_to(root) if source.is_relative_to(root) else source
        status = '設定済み' if config_ready(config) else 'Vf・近似領域の設定が必要'
        print(f"{number}: {display} | CSVデータ行: {config['analysis_rows']} [{status}]")
        print(f'   設定: {path.relative_to(root)}')
    print('0: キャンセル')
    while True:
        try:
            choice = input('番号を入力してEnter: ').strip()
        except EOFError as error:
            raise ValueError('番号を読み取れません。--configで設定JSON、または--latestで最新設定を指定してください。') from error
        if choice == '0':
            print('キャンセルしました。')
            return None
        if choice.isdecimal() and 1 <= int(choice) <= len(available):
            return available[int(choice) - 1][0]
        print(f'0～{len(available)}の番号を入力してください。')


def selection_preview(out, rows, x, y, trace):
    """近似領域が未設定でも、判断に必要なグラフと行番号対応表を保存する。"""
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    write_point_csv(out / 'points.csv', rows, {
        'Vout [V]': x, 'Ipol [A]': y, 'イオン近似 [A]': trace.ion_current,
        '補正Ie [A]': trace.electron_current, 'ln(|Ie| / 1 A)': trace.log_current,
        '隣接差分 [A/V]': trace.derivative}, row_label='解析行')
    figure = Figure(figsize=(13, 8), layout='constrained')
    FigureCanvasAgg(figure)
    axes = figure.subplots(2, 2).flat
    positive = x > 0
    for ax, values, title, label in zip(axes,
            (y, trace.electron_current, trace.log_current, trace.derivative),
            ('Ipol and ion fit', 'Ion-subtracted current', 'Select fit regions from log current', 'Adjacent difference'),
            ('Ipol (A)', 'Ie (A)', 'ln(|Ie| / 1 A)', 'abs(delta Ie/delta V) (A/V)')):
        mask = positive & np.isfinite(values)
        if title == 'Adjacent difference':
            mask &= vp0_interval_mask(x)
        ax.plot(x[mask], values[mask], 'o', linestyle='None', ms=3)
        ax.set(title=title, xlabel='Vout (V)', ylabel=label, xlim=(0, None))
        ax.grid(alpha=.2)
    axes = figure.axes
    xx = np.sort(x[positive])
    axes[0].plot(xx, trace.ion_fit.at(xx), '-', label='Ion fit from negative voltage')
    axes[0].legend(fontsize=8)
    peak = x[trace.peak_index]
    if peak > 0:
        for ax in axes[2:]:
            ax.axvline(peak, color='#b4473b', ls='-.', label=f'Vp0={peak:.6g} V')
            ax.legend(fontsize=8)
    figure.savefig(out / 'preview.png', dpi=160)
    figure.clear()
    print(f'確認用グラフ: {out / "preview.png"}\n行番号の対応表: {out / "points.csv"}')


def configure_terminal(source, config, *, preview_out=None):
    """GUIを開かず、利用者の判断したVf方法と近似行を対話入力して検証する。"""
    config = dict(config)
    rows, x, y = read_points(source, config)
    trace = correct_trace(x, y, rows)
    print('\nVf・近似領域をターミナルで設定します。0でキャンセルできます。')
    if preview_out is not None:
        selection_preview(preview_out, rows, x, y, trace)
    else:
        print('保存なしのため、正電圧の行番号とln値を表示します。')
        print('解析行, Vout [V], ln(|Ie| / 1 A)')
        for row, voltage, value in zip(rows, x, trace.log_current):
            if voltage > 0 and np.isfinite(value):
                print(f'{row}, {voltage:.9g}, {value:.9g}')
    peak = float(x[trace.peak_index])
    print(f'Vp0 = {peak:.9g} V（正電圧の補正電流・ln変換前の隣接差分の最大区間）')
    index = trace.peak_index
    print(f'最大区間: 解析行 {int(rows[index])} → {int(rows[index+1])}、'
          f'|ΔVout| = {abs(float(x[index+1]-x[index])):.9g} V')
    modes = {'1': 'minimum', '2': 'lowest_two'}
    for choice, mode in modes.items():
        try:
            vf = minimum_floating_potential(x, trace.log_current, rows,
                                           average_two=mode == 'lowest_two', positive_only=True)
            label = '正電圧のln最小実測点' if choice == '1' else 'ln値が小さい2点の電圧平均'
            print(f'{choice}: {label} → Vf = {vf.voltage_V:.9g} V')
        except ValueError as error:
            print(f'{choice}: {error}')

    def ask(prompt):
        try:
            return input(prompt).strip()
        except EOFError as error:
            raise ValueError('設定の入力を読み取れません。対話ターミナルで入力するか、--config/解析条件の引数を指定してください。') from error

    while True:
        default = config.get('floating_potential_mode')
        answer = ask(f'Vfの方法（1/2{", Enterで保存済みの方法" if default in modes.values() else ""}）: ')
        if answer == '0':
            return None
        mode = modes.get(answer, default if not answer else answer)
        if mode not in modes.values():
            print('1または2を入力してください。')
            continue
        try:
            floating = minimum_floating_potential(x, trace.log_current, rows,
                                                 average_two=mode == 'lowest_two', positive_only=True)
        except ValueError as error:
            print(error)
            continue
        config['floating_potential_mode'] = mode
        break

    while True:
        for key, label in (('retarding', '電子減速領域'), ('saturation', '電子飽和領域')):
            valid = (x > 0) & np.isfinite(trace.log_current)
            if key == 'retarding':
                valid &= (x >= floating.voltage_V) & (x < peak)
            candidates = rows[valid]
            if len(candidates) < 2:
                print(f'{label}を選べる有効点が2点未満です。Vf={floating.voltage_V:.9g} V、Vp0={peak:.9g} V。')
                print(f'現在の解析範囲: {config["analysis_rows"]}。確認図・points.csvから対象の掃引を確認してください。')
                print('Vp0の計算方法は変更しません。解析する行範囲を変更するか、0でキャンセルしてください。')
                while True:
                    answer = ask('新しい解析範囲の開始行 終了行: ')
                    if answer == '0':
                        return None
                    try:
                        pair = [int(v) for v in answer.replace(',', ' ').replace('、', ' ').split()]
                        candidate = config | {'analysis_rows': pair}
                        new_rows, new_x, new_y = read_points(source, candidate)
                        correct_trace(new_x, new_y, new_rows)
                        # 範囲が変わったときは、以前のVf・近似領域を自動流用しない。
                        candidate.update(floating_potential_mode='unselected', retarding_fit_rows=None,
                                         saturation_fit_rows=None)
                        return candidate
                    except (ValueError, TypeError) as error:
                        print(f'解析範囲を確認してください: {error}')
            print(f'{label}: 候補行 {candidates.tolist()}')
            print(f'電圧条件: {floating.voltage_V:.9g} V以上、{peak:.9g} V未満'
                  if key == 'retarding' else '電圧条件: Vout > 0（Vp0による制限なし）')
            while True:
                default = config.get(f'{key}_fit_rows')
                answer = ask(f'{label}の開始行 終了行（空白区切り{f", Enterで{default}" if default else ""}）: ')
                if answer == '0':
                    return None
                try:
                    pair = default if not answer else [int(v) for v in answer.replace(',', ' ').replace('、', ' ').split()]
                    fit = fit_log_region(x, rows, trace, pair, key, floating.voltage_V)
                    if key == 'retarding' and fit.slope <= 0:
                        raise ValueError('電子減速領域は傾きが正になる範囲を選んでください。')
                    config[f'{key}_fit_rows'] = list(pair)
                    print(f'近似式: ln(|Ie| / 1 A) = {fit.slope:.9g} × Vout {fit.intercept:+.9g}')
                    break
                except (ValueError, TypeError) as error:
                    print(f'入力を確認してください: {error}')
        try:
            calculate_parameters(x, rows, trace, config)
            return config
        except (ValueError, OverflowError) as error:
            print(f'この組合せでは計算できません: {error}\n減速・飽和領域を入力し直してください。')


def parse_arguments(argv):
    """実行オプションの定義。mainでは設定・計算・保存の流れだけを扱う。"""
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--config', type=Path, help='設定JSONを指定して計算')
    selection.add_argument('--select', action='store_true', help='入力データを番号で選ぶ（対話ターミナルでは標準）')
    selection.add_argument('--latest', action='store_true', help='一覧を出さず最新の保存済み設定で計算')
    parser.add_argument('--input', type=Path, help='入力CSV。--configなしでは解析条件も直接指定')
    # GUIの保存設定がなくても、実測範囲と手動で判断した近似範囲を直接渡せる。
    parser.add_argument('--analysis-rows', type=int, nargs=2, metavar=('START', 'END'),
                        help='解析する単一掃引の開始・終了行')
    parser.add_argument('--vf-mode', dest='floating_potential_mode', choices=('minimum', 'lowest_two'),
                        help='Vf: minimum=正電圧のln最小実測点、lowest_two=小さい2点の電圧平均')
    parser.add_argument('--retarding-rows', dest='retarding_fit_rows', type=int, nargs=2,
                        metavar=('START', 'END'), help='電子減速領域の開始・終了行')
    parser.add_argument('--saturation-rows', dest='saturation_fit_rows', type=int, nargs=2,
                        metavar=('START', 'END'), help='電子飽和領域の開始・終了行')
    parser.add_argument('--probe-radius', dest='probe_radius_m', type=float, help='球状プローブの半径[m]')
    parser.add_argument('--applicability-min', dest='applicability_min_V', type=float,
                        help='Vin＋Voutの適用下限[V]（標準-10、境界は含めない）')
    parser.add_argument('--applicability-max', dest='applicability_max_V', type=float,
                        help='Vin＋Voutの適用上限[V]（標準10、境界は含めない）')
    parser.add_argument('--resistance-ohm', dest='resistance_ohm', type=float,
                        help='A側抵抗が空欄の場合の抵抗[Ω]（標準1000000、CSVのL列を優先）')
    parser.add_argument('--configure', action='store_true', help='確認図を保存し、Vf方法・近似範囲をターミナルで入力')
    parser.add_argument('--output-dir', type=Path, help='結果CSV・全グラフの保存先を指定（標準: outputs内）')
    parser.add_argument('--no-export', action='store_true', help='結果を表示するだけで保存しない')
    return parser.parse_args(argv)


def main(argv=None, *, root=None):
    """設定 → 必要なら対話入力 → 計算 → 表示・保存。GUIは起動しない。"""
    args = parse_arguments(argv)
    try:
        # 1. 入力ファイルと、そのデータ専用の解析設定を決める。
        terminal = sys.stdin.isatty() and sys.stdout.isatty()
        allow_setup = args.configure or args.select or terminal
        overrides = {key: getattr(args, key) for key in (
            'analysis_rows', 'floating_potential_mode', 'retarding_fit_rows',
            'saturation_fit_rows', 'probe_radius_m', 'applicability_min_V',
            'applicability_max_V', 'resistance_ohm') if getattr(args, key) is not None}
        direct_input = args.input is not None and args.config is None and not (args.latest or args.select)
        if direct_input:
            # 入力だけを変えて他データの近似範囲を流用しない。4条件は明示指定を必須とする。
            required = {'analysis_rows': '--analysis-rows', 'floating_potential_mode': '--vf-mode',
                        'retarding_fit_rows': '--retarding-rows', 'saturation_fit_rows': '--saturation-rows'}
            missing = [option for key, option in required.items()
                       if key not in overrides and (key == 'analysis_rows' or not allow_setup)]
            if missing:
                raise ValueError('GUIなしの直接実行には ' + '・'.join(missing)
                                 + ' を指定してください。設定JSONがあれば--configで指定できます。')
            path = None
            source = args.input.resolve()
            config = normalize_config({'input': str(source), **overrides})
        else:
            interactive = args.config is None and not args.latest and args.input is None and terminal
            path = (select_config(root=root) if args.select or interactive
                    else choose_config(args.config, root=root))
            if path is None:
                return 0
            config = normalize_config(load_config(path) | overrides)
            source = (args.input or path.parent / config['input']).resolve()
        # 上下限だけを変更した場合は、現在の掃引と重なる適用点へ範囲を広げる/絞る。
        # --analysis-rowsも明示された場合はその行指定を優先し、適用内か検証する。
        if any(key in overrides for key in ('applicability_min_V', 'applicability_max_V')) and 'analysis_rows' not in overrides:
            pair = select_applicable_rows(source, config)
            if pair != config['analysis_rows']:
                config = config | {'analysis_rows': pair}
                for key in ('floating_potential_mode', 'retarding_fit_rows', 'saturation_fit_rows'):
                    config[key] = overrides.get(key, 'unselected' if key == 'floating_potential_mode' else None)
        print(f"適用範囲: {config['applicability_min_V']:g} < Vin＋Vout < {config['applicability_max_V']:g} V")
        # 2. Vf方法・近似範囲が不足していれば、確認図を見ながら利用者が指定する。
        configured = False
        if args.configure or (allow_setup and not config_ready(config)):
            preview = None if args.no_export else (
                args.output_dir or output_directory(source, config, path, root=root)) / 'selection_preview'
            if preview is not None and source.resolve() == (preview / 'points.csv').resolve():
                raise ValueError('確認用CSVの保存先が入力ファイルと重なっています。別の出力先を指定してください。')
            while True:
                config = configure_terminal(source, config, preview_out=preview)
                if config is None:
                    print('キャンセルしました。解析条件は変更していません。')
                    return 0
                if config_ready(config):
                    break
            configured = True
        # 3. 同じ計算関数を使い、Vf・Vp0・Vp・Te・Ies・neを求めて表示する。
        result = calculate(source, config)
        setting_label = path if path is not None else 'コマンドライン指定（設定JSON・GUI不要）'
        print(f'使用設定: {setting_label}\n入力: {source}')
        for key, label, unit in [
            ('floating_potential_V', 'Vf', 'V'),
            ('derivative_peak_voltage_V', 'Vp0', 'V'),
            ('plasma_potential_V', 'Vp', 'V'),
            ('electron_temperature_K', '電子温度', 'K'),
            ('electron_temperature_eV', '電子温度', 'eV'),
            ('electron_saturation_current_A', '電子飽和電流', 'A'),
            ('electron_density_cm3', '電子密度', 'cm^-3'),
        ]:
            print(f'{label}: {result.metrics[key]:.9g} {unit}')
        # 4. 保存が有効ならCSV・PNGを出力する。入力ファイルは変更しない。
        if not args.no_export:
            out = (args.output_dir or output_directory(source, config, path, root=root)).resolve()
            save_results(out, result, source, config, path)
            print(f'保存先: {out}')
            if configured and path is not None and args.input is None:
                # 結果を出力できた後に、利用者が入力した条件だけを対象設定へ保存する。
                temporary = path.with_suffix('.json.tmp')
                temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
                temporary.replace(path)
                print(f'解析条件を保存しました: {path}')
        return 0
    except KeyboardInterrupt:
        print('\nキャンセルしました。')
        return 130
    except (OSError, ValueError, KeyError, TypeError, OverflowError, ImportError, csv.Error) as error:
        print(f'解析エラー: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

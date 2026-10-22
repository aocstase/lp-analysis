"""計算結果のCSV・画像・Excelレポート・JSON出力。GUIは使用しない。"""
from __future__ import annotations
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import TYPE_CHECKING

import numpy as np
import openpyxl

from excel_output import write_workbook
from lp_report import write_report
from calculation import normalize_config

if TYPE_CHECKING:
    from analyze import Analysis

def write_csvs(out: Path, result: Analysis):
    with (out / 'analysis.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(['excel_row', 'voltage_V', 'Ipol_A', 'ion_baseline_A', 'Ie_A',
                         'ln_abs_Ie', 'dIe_dV_A_per_V', 'Ie_positive', 'valid_derivative'])
        for i, r in enumerate(result.rows):
            d = result.derivative[i]
            writer.writerow([int(r), result.voltage[i], result.current[i], result.ion_current[i],
                             result.electron_current[i], float(result.log_current[i]) if np.isfinite(result.log_current[i]) else '',
                             float(d) if np.isfinite(d) else '', int(result.electron_current[i] > 0), int(np.isfinite(d))])
    with (out / 'iv_all.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(['excel_row', 'raw_excel_row', 'index', 'sweep', 'voltage_V', 'Ipol_A', 'Vin_plus_Vout_V', 'applicable'])
        for r in result.all_rows:
            writer.writerow([r, r-1, result.values[f'A{r-1}'], result.values.get(f'B{r-1}',''),
                             *[result.values[f'{c}{r}'] for c in ('O', 'P', 'Q', 'V')]])

def plot_results(out: Path, result: Analysis):
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    fig = Figure(figsize=(16,8.5),layout='constrained')
    FigureCanvasAgg(fig)
    axes = fig.subplots(2,3)
    positive=result.voltage>0
    x,y,electron,log=(a[positive] for a in (result.voltage,result.current,result.electron_current,result.log_current))
    rows=result.rows[positive]
    ax = axes[0, 0]
    for sweep in dict.fromkeys(result.values.get(f'B{r-1}','') for r in result.all_rows):
        rr = [r for r in result.all_rows if result.values.get(f'B{r-1}','') == sweep and result.values[f'O{r}']>0]
        ax.plot([result.values[f'O{r}'] for r in rr], [result.values[f'P{r}'] * 1e6 for r in rr], 'o',linestyle='None',ms=3,label=sweep)
    ax.scatter(x, y*1e6, s=12, color='black', label='Selected rows', zorder=3)
    ax.set(title='Raw I-V (Vout > 0)', ylabel='Ipol (uA)'); ax.legend()
    ax = axes[0, 1]
    ax.plot(x, y*1e6, 'o',linestyle='None', ms=3, label='Ipol')
    ax.plot(x,result.ion_current[positive]*1e6,'-',lw=2.3,label='Ion fit from Vout < 0')
    ax.set(title='Ion subtraction', ylabel='Current (uA)'); ax.legend()
    ax = axes[0, 2]
    ax.plot(x, electron*1e6, 'o',linestyle='None', ms=3)
    ax.axhline(0, color='gray', lw=.8)
    ax.set(title='Corrected electron current', ylabel='Ie (uA)')
    ax = axes[1, 0]
    ax.plot(x, log, 'o',linestyle='None', ms=3, color='#536278', label='ln(abs(Ie))')
    for key, label, color in [('retarding', 'Retarding', '#1c6eb5'), ('saturation', 'Saturation', '#c56720'), ('vp', 'Vp evaluation', '#418153')]:
        fit = result.fits[key]; sel = (rows >= fit.rows[0]) & (rows <= fit.rows[1])
        if not np.any(sel):continue
        ax.scatter(x[sel], log[sel], s=30, color=color)
        xx = np.linspace(max(float(x.min()),min(x[sel].min(),result.metrics['plasma_potential_V'])),max(x[sel].max(),result.metrics['plasma_potential_V']),60)
        ax.plot(xx, fit.at(xx), '-',lw=2.3,color=color, label=f'{label} [{fit.rows[0]}:{fit.rows[1]}]')
    vp=result.metrics['plasma_potential_V']
    ax.scatter(vp,result.fits['retarding'].at(vp),marker='*',s=110,color='black',label='Vp intersection')
    ax.scatter(vp,result.metrics['log_electron_saturation_current'],marker='D',s=35,color='#418153',label='Ies at Vp')
    ax.set(title='Log current and refitted lines', ylabel='Ipol (A)'); ax.legend(fontsize=8)
    if result.floating_potential.voltage_V>0:ax.axvline(result.floating_potential.voltage_V,color='#943a94',ls=':',label='Vf (positive Vout)')
    ax.legend(fontsize=8)
    ax = axes[1, 1]
    d=result.derivative[positive];finite=np.isfinite(d)
    ax.plot(x[finite],d[finite]*1e6,'o',linestyle='None',ms=3)
    if result.metrics['derivative_peak_voltage_V']>0:ax.axvline(result.metrics['derivative_peak_voltage_V'],ls='--',color='gray',label='Maximum (left point)')
    ax.set(title='Adjacent difference of Ie', ylabel='dIe/dV (uA/V)'); ax.legend(fontsize=8)
    ax = axes[1, 2]; ax.axis('off')
    m = result.metrics
    text = (f"Recalculated results\n\nTe = {m['electron_temperature_K']:,.2f} K\n"
            f"Te = {m['electron_temperature_eV']:.6f} eV\nVf = {m['floating_potential_V']:.6f} V\nVp = {m['plasma_potential_V']:.6f} V\n"
            f"Ies = {m['electron_saturation_current_A']:.6e} A\nne = {m['electron_density_m3']:.6e} /m3\n"
            f"ne = {m['electron_density_cm3']:,.3f} /cm3\n\nFit ranges are selected manually.\n"
            "Coefficients use full precision.\nTwo-point fits do not validate the model.")
    ax.text(.05, .95, text, va='top', fontsize=11, linespacing=1.6)
    for ax in axes.flat[:5]:
        ax.set_xlabel('Vout (V)');ax.set_xlim(left=0);ax.grid(alpha=.2)
    fig.savefig(out / 'analysis.png', dpi=160)
    fig.savefig(out / 'analysis.svg')
    fig.clear()

def export_analysis(source: Path, config: dict, out: Path, result: Analysis | None = None):
    config = normalize_config(config)
    if source.resolve() in ((out/'analysis_corrected.xlsx').resolve(), (out/'analysis_report.xlsx').resolve()):
        raise ValueError('入力ファイルは上書きできません。別の出力先を指定してください。')
    if result is None:
        from analyze import analyze
        result = analyze(source, config)
    out.mkdir(parents=True, exist_ok=True)
    write_workbook(source, out / 'analysis_corrected.xlsx', result)
    write_csvs(out, result)
    plot_results(out, result)
    write_report(out/'analysis_report.xlsx',result)
    summary = {
        'source': str(source), 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'correction_formula': result.formulas[f"AE{config['analysis_rows'][0]}"], 'config': config,
        'metrics': result.metrics, 'fits': {k: asdict(v) for k,v in result.fits.items()},
        'floating_potential': result.floating_potential.to_dict(), 'notes':result.notes,
        'software': {'python':sys.version,'numpy':np.__version__,'openpyxl':openpyxl.__version__},
    }
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    return result

"""解析段階を確認できる、ネイティブExcelグラフ付きの数値レポート。"""
from __future__ import annotations

import math

import openpyxl
from openpyxl.chart import Reference, ScatterChart, Series
from openpyxl.styles import Font, PatternFill


def make_chart(sheet,title,x_column,y_columns,anchor,*,y_title='Current [A]',fit_columns=(),data_sheet=None):
    chart=ScatterChart()
    chart.title=title;chart.x_axis.title='Vout [V]';chart.y_axis.title=y_title
    chart.style=13;chart.width=19;chart.height=11
    chart.scatterStyle='lineMarker'
    chart.x_axis.scaling.min=0
    data_sheet=data_sheet or sheet
    x=Reference(data_sheet,min_col=x_column,min_row=2,max_row=data_sheet.max_row)
    for column in y_columns:
        y=Reference(data_sheet,min_col=column,min_row=1,max_row=data_sheet.max_row)
        series=Series(y,x,title_from_data=True)
        if column in fit_columns:
            series.marker.symbol='none'
            series.graphicalProperties.line.width=28575
            series.graphicalProperties.line.solidFill={4:'246CAF',8:'246CAF',9:'CE721B',10:'398147'}[column]
            series.graphicalProperties.line.prstDash='solid'
        else:
            series.marker.symbol='circle';series.marker.size=4
            series.graphicalProperties.line.noFill=True
        series.smooth=False
        chart.series.append(series)
    sheet.add_chart(chart,anchor)


def positive_chart_data(book,sheet):
    target=book.create_sheet('_正電圧_'+sheet.title[:24]);target.sheet_state='hidden'
    for i,row in enumerate(sheet.iter_rows(values_only=True)):
        if i==0 or isinstance(row[1],(int,float)) and row[1]>0:target.append(row)
    return target


def write_report(destination,result):
    """再計算済み数値を収録。再実行時はPython/analysis_corrected.xlsxを使う。"""
    book=openpyxl.Workbook();summary=book.active;summary.title='結果'
    summary.append(['項目','値'])
    labels={
        'floating_potential_V':'Vf [V]', 'derivative_peak_voltage_V':'Vp0 [V]',
        'plasma_potential_V':'Vp [V]', 'electron_temperature_K':'Te [K]',
        'electron_temperature_eV':'Te [eV]', 'electron_saturation_current_A':'Ies [A]',
        'electron_mean_speed_m_per_s':'電子熱運動平均速度 [m/s]',
        'probe_area_m2':'球状プローブ表面積 [m²]',
        'electron_density_m3':'ne [m^-3]', 'electron_density_cm3':'ne [cm^-3]',
    }
    for key,label in labels.items():summary.append([label,result.metrics[key]])
    summary.append(['Vfの方法',result.floating_potential.method])
    for name,fit in result.fits.items():
        summary.append([f'{name}: 傾き',fit.slope])
        summary.append([f'{name}: 切片',fit.intercept])
        summary.append([f'{name}: 使用行',f'{fit.rows[0]}:{fit.rows[1]} ({fit.points}点)'])
    for note in result.notes:summary.append(['計算条件',note])
    summary.append(['レポートについて','計算済み数値の記録。再計算用の式はanalysis_corrected.xlsx、設定はsummary.jsonを参照。'])
    raw=book.create_sheet('全測定');applicable=book.create_sheet('適用範囲')
    for sheet in (raw,applicable):sheet.append(['元Excel行','Vout [V]','Ipol [A]','Vin+Vout [V]','適用判定','sweep'])
    for row in result.all_rows:
        values=[row,result.values[f'O{row}'],result.values[f'P{row}'],result.values[f'Q{row}'],
                result.values[f'V{row}'],result.values.get(f'B{row-1}','')]
        raw.append(values)
        if values[4]:applicable.append(values)
    raw_positive=positive_chart_data(book,raw);applicable_positive=positive_chart_data(book,applicable)
    make_chart(raw,'1. Ipol–Vout（Vout > 0）',2,[3],'H2',data_sheet=raw_positive)
    make_chart(applicable,'2. 適用範囲（Vout > 0）',2,[3],'H2',data_sheet=applicable_positive)
    selected=book.create_sheet('選択掃引とイオン補正')
    selected.append(['元Excel行','Vout [V]','Ipol [A]','イオン近似 [A]','補正Ie [A]',
                     'ln(abs(Ie/1 A))','ΔIe/ΔV [A/V]',
                     '減速近似 ln(Ie)','飽和近似 ln(Ie)','Vp近傍近似 ln(Ie)'])
    for i,row in enumerate(result.rows):
        def finite(value):return float(value) if math.isfinite(float(value)) else None
        extra=[]
        for key in ('retarding','saturation','vp'):
            fit=result.fits[key]
            use=fit.rows[0]<=row<=fit.rows[1]
            if key in ('retarding','saturation'):
                fit_x=result.voltage[(result.rows>=fit.rows[0])&(result.rows<=fit.rows[1])]
                vp=result.metrics['plasma_potential_V']
                below=result.voltage[result.voltage<=vp];above=result.voltage[result.voltage>=vp]
                left=min(float(fit_x.min()),float(below.max()) if len(below) else vp)
                right=max(float(fit_x.max()),float(above.min()) if len(above) else vp)
                use=left<=result.voltage[i]<=right
            extra.append(float(fit.at(result.voltage[i])) if use else None)
        selected.append([int(row),float(result.voltage[i]),float(result.current[i]),float(result.ion_current[i]),
                         float(result.electron_current[i]),finite(result.log_current[i]),finite(result.derivative[i]),*extra])
    selected_positive=positive_chart_data(book,selected)
    make_chart(selected,'3. 負電圧から計算したイオン近似',2,[3,4],'L2',fit_columns=(4,),data_sheet=selected_positive)
    make_chart(selected,'4. イオン差し引き後',2,[5],'L24',data_sheet=selected_positive)
    make_chart(selected,'5. ln表示と選択近似領域',2,[6,8,9,10],'W2',y_title='Ipol (A)',fit_columns=(8,9,10),data_sheet=selected_positive)
    make_chart(selected,'6. Vp0決定用の隣接差分',2,[7],'W24',y_title='ΔIe/ΔV [A/V]',data_sheet=selected_positive)
    for sheet in book:
        sheet.freeze_panes='B2';sheet.auto_filter.ref=sheet.dimensions
        for cell in sheet[1]:
            cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='244C67')
        for column in range(1,sheet.max_column+1):
            sheet.column_dimensions[openpyxl.utils.get_column_letter(column)].width=24
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                if isinstance(cell.value,float):cell.number_format='0.000000E+00'
    summary.column_dimensions['A'].width=35;summary.column_dimensions['B'].width=100
    # 外部由来のsweep等の文字列は数式として扱わない。
    for sheet in book:
        for row in sheet:
            for cell in row:
                if isinstance(cell.value,str):cell.data_type='s'
    try:book.save(destination)
    finally:book.close()

"""Excelの数式・計算結果・既存チャートを保存する処理。

科学計算はcalculation.pyにあり、このファイルはその結果の書き込みだけを担当する。
元の書式・共有数式・グラフ部品を保つため、xlsx内の必要なXMLだけを更新する。
"""
from __future__ import annotations
import math
from pathlib import Path
import re
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

import numpy as np
import openpyxl

from calculation import DEFAULT_PARAMETERS, ROUNDOFF_FACTOR

if TYPE_CHECKING:
    from analyze import Analysis

CHART_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"
NS = {"c": CHART_NS}


def prepare_excel_result(result, measurement, config):
    """計算済みの値と同じ意味のExcel数式を用意する。数値計算のやり直しはしない。"""
    values, formulas = result.values, result.formulas
    occupied = set(measurement.values) | set(measurement.original_formulas)

    def assign(cell, formula, value):
        formulas[cell], values[cell] = formula, value

    # 生データと同じ行対応で変換値・適用判定を保存する。
    for i, row in enumerate(result.all_rows):
        raw = row - 1
        for cell, formula, value in [
            (f"O{row}", f"=-H{raw}", measurement.all_voltage[i]),
            (f"P{row}", f"=-F{raw}/10^6", measurement.all_current[i]),
            (f"Q{row}", f"=P{row}*10^6+O{row}", measurement.total[i]),
            (f"T{row}", f"=IF($S$3<Q{row},1,0)", int(measurement.total[i] > -10)),
            (f"U{row}", f"=IF(Q{row}<$R$3,1,0)", int(measurement.total[i] < 10)),
            (f"V{row}", f"=IF(AND(T{row}=1,U{row}=1),1,0)", int(measurement.applicable[i])),
        ]:
            assign(cell, formula, float(value))
    for cell, value in (("R3", 10.), ("S3", -10.)):
        if values.get(cell) != value:
            assign(cell, f"={decimal(value)}", value)

    start, end = map(int, result.rows[[0, -1]])
    ion_fit = result.fits["ion"]
    lo, hi = ion_fit.rows
    ion_xy = f"$P${lo}:$P${hi},$O${lo}:$O${hi}"
    ion_a, ion_b = f"SLOPE({ion_xy})", f"INTERCEPT({ion_xy})"
    if ion_fit.points != hi - lo + 1:
        # 0 V付近で電圧が往復するデータでも、負電圧点だけで近似する式を出す。
        ox, py = f"$O${start}:$O${end}", f"$P${start}:$P${end}"
        count = f'COUNTIF({ox},"<0")'
        sx, sy = f'SUMIF({ox},"<0",{ox})', f'SUMIF({ox},"<0",{py})'
        sxx = f"SUMPRODUCT(({ox}<0)*{ox}*{ox})"
        sxy = f"SUMPRODUCT(({ox}<0)*{ox}*{py})"
        ion_a = f"(({count}*{sxy}-{sx}*{sy})/({count}*{sxx}-{sx}^2))"
        ion_b = f"(({sy}-{ion_a}*{sx})/{count})"

    for i, row in enumerate(result.rows):
        residual = f"P{row}-({ion_a}*O{row}+{ion_b})"
        threshold = f"{ROUNDOFF_FACTOR:.17g}*(ABS(P{row})+ABS({ion_a}*O{row})+ABS({ion_b}))"
        assign(f"AE{row}", f"=IF(ABS({residual})<={threshold},0,{residual})", float(result.electron_current[i]))
        assign(f"AF{row}", f'=IF(AE{row}=0,"",LN(ABS(AE{row})))',
               float(result.log_current[i]) if np.isfinite(result.log_current[i]) else "")
        assign(f"AG{row}", f"=O{row}", float(result.voltage[i]))
        formula = f"=ABS(AE{row+1}-AE{row})/ABS(AG{row+1}-AG{row})" if i+1 < len(result.rows) else '=""'
        assign(f"AH{row}", formula, float(result.derivative[i]) if np.isfinite(result.derivative[i]) else "")

    vf_cell = config.get("vf_output_cell", "AE154")
    minimum_cell = config.get("log_min_output_cell", "AF154")
    if vf_cell in formulas or minimum_cell in formulas:
        raise ValueError("Vf・ln最小値の出力セルは解析データの範囲外にしてください。")
    result.vf_output_cell, result.log_min_output_cell = vf_cell, minimum_cell
    for cell, label in ((vf_cell, "Vf [V]"), (minimum_cell, "min ln|Ie|")):
        row, column = openpyxl.utils.cell.coordinate_to_tuple(cell)
        result.text_updates[f"{openpyxl.utils.get_column_letter(column)}{row-1}"] = label
    assign(vf_cell, result.floating_potential.formula, result.floating_potential.voltage_V)
    eligible = [f"AF{r}" for r, x, y in zip(result.rows, result.voltage, result.log_current) if x > 0 and np.isfinite(y)]
    groups = ["MIN(" + ",".join(eligible[i:i+200]) + ")" for i in range(0, len(eligible), 200)]
    minimum_formula = "=" + groups[0] if len(groups) == 1 else "=MIN(" + ",".join(groups) + ")"
    assign(minimum_cell, minimum_formula, result.floating_potential.minimum_log_current)
    for row in range(124, 131):
        assign(f"AI{row}", "=" + openpyxl.utils.absolute_coordinate(vf_cell), result.floating_potential.voltage_V)

    def xy(key):
        lo, hi = result.fits[key].rows
        return f"$AF${lo}:$AF${hi},$O${lo}:$O${hi}"

    baseline = config.get("ion_equation_cell", "AE121")
    result.text_updates[baseline] = f"y = {ion_fit.slope:.5E}x {ion_fit.intercept:+.5E}"
    for cell, key in (("AL130", "retarding"), ("AL132", "saturation"), ("AL133", "vp")):
        fit = result.fits[key]
        equation = (f'= "y = "&TEXT(SLOPE({xy(key)}),"0.00000E+00")&"x "'
                    f'&TEXT(INTERCEPT({xy(key)}),"+0.00000E+00;-0.00000E+00;0")')
        assign(cell, equation, f"y = {fit.slope:.5E}x {fit.intercept:+.5E}")

    # ExcelでもTe・Vp・Ies・密度の依存関係が分かるよう、結果だけでなく数式を残す。
    m = result.metrics
    constants = DEFAULT_PARAMETERS | config
    for cell, formula, value in [
        ("AK123", f"=4*PI()*{decimal(constants['probe_radius_m'])}^2", m["probe_area_m2"]),
        ("AL123", f"={constants['electron_mass_kg']:.12g}", constants["electron_mass_kg"]),
        ("AK125", f"={constants['boltzmann_J_per_K']:.12g}", constants["boltzmann_J_per_K"]),
        ("AL125", f"={constants['electron_charge_C']:.12g}", constants["electron_charge_C"]),
        ("AM125", f"=SLOPE({xy('retarding')})", result.fits["retarding"].slope),
        ("AK128", "=AL125/AK125/AM125", m["electron_temperature_K"]),
        ("AL128", "=SQRT(8*AK125*AK128/(PI()*AL123))", m["electron_mean_speed_m_per_s"]),
        ("AM128", f"=(INTERCEPT({xy('saturation')})-INTERCEPT({xy('retarding')}))/(SLOPE({xy('retarding')})-SLOPE({xy('saturation')}))", m["plasma_potential_V"]),
        ("AN128", f"=SLOPE({xy('vp')})*AM128+INTERCEPT({xy('vp')})", m["log_electron_saturation_current"]),
        ("AO128", "=EXP(AN128)", m["electron_saturation_current_A"]),
        ("AP128", "=4*AO128/(AL125*AK123*AL128)", m["electron_density_m3"]),
        ("AQ128", "=AP128/10^6", m["electron_density_cm3"]),
        ("AQ132", "=LOG10(AQ128)", m["density_log10_cm3"]),
    ]:
        assign(cell, formula, value)
    labels = {
        'O2':'Vout [V]', 'P2':'Ipol [A]', 'Q2':'Vin+Vout', 'R2':'Vout上限 [V]', 'S2':'Vout下限 [V]',
        'T2':'負領域', 'U2':'正領域', 'V2':'総合',
        'AK122':'球面積 S [m^2]', 'AL122':'電子質量 me [kg]',
        'AK124':'ボルツマン定数 k', 'AL124':'電荷 e [C]', 'AM124':'d ln(Ie) / dV [1/V]',
        'AK127':'電子温度 Te [K]', 'AL127':'平均電子速度 [m/s]', 'AM127':'空間電位 Vp [V]',
        'AN127':'ln(Ies)', 'AO127':'電子飽和電流 Ies [A]', 'AP127':'電子密度 [/m^3]', 'AQ127':'電子密度 [/cm^3]',
        'AK130':'電子減速領域の近似', 'AK132':'電子飽和領域の近似', 'AK133':'Ies評価用の近似', 'AQ131':'log10(ne [/cm^3])',
    }
    result.text_updates.update({cell: label for cell, label in labels.items() if cell not in occupied})
    values.update(result.text_updates)
    result.added_cells = ({vf_cell, minimum_cell} | set(result.text_updates) | set(formulas)) - occupied
    return result

def decimal(value: float) -> str:
    return np.format_float_positional(float(value), trim="-")

def corrected_formula(row: int, slope: float, intercept: float) -> str:
    sign = "-" if intercept < 0 else "+"
    return f"=P{row}-({decimal(slope)}*O{row} {sign} {decimal(abs(intercept))})"

def update_cell_xml(xml: str, cell: str, value, formula: str | None, preserve_formula=False) -> str:
    """対象セルだけを置換。openpyxlによるブック全体の再保存を避ける。"""
    pattern = re.compile(r'<c\b(?=[^>]*\br="' + re.escape(cell) + r'")[^>]*?(?:/>|>.*?</c>)', re.DOTALL)

    def replace(match):
        opening = match.group(0).split(">", 1)[0].rstrip("/")
        opening = re.sub(r'\s+t="[^"]*"', "", opening)
        if isinstance(value, str):
            if formula is None:
                return opening + ' t="inlineStr"><is><t>' + escape(value) + "</t></is></c>"
            return opening + ' t="str"><f>' + escape(formula.lstrip("=")) + "</f><v>" + escape(value) + "</v></c>"
        if formula and preserve_formula:
            # 展開後の式が同じ場合は、元の共有数式のmaster/ref/si属性を保つ。
            content = re.search(r'<f\b[^>]*?(?:/>|>.*?</f>)', match.group(0), re.DOTALL).group(0)
        else:
            content = f"<f>{escape(formula.lstrip('='))}</f>" if formula else ""
        return opening + ">" + content + f"<v>{float(value):.17g}</v></c>"

    updated, count = pattern.subn(replace, xml)
    if count != 1:
        raise ValueError(f"出力テンプレートに{cell}が1個存在する必要があります（{count}個）。")
    return updated

def range_values(reference: str, result: Analysis):
    sheet, cells = reference.rsplit("!", 1)
    if sheet.strip("'").replace("''", "'") != result.sheet:
        raise ValueError(f"未知のグラフ参照: {reference}")
    cells = cells.replace("$", "")
    a, _, b = cells.partition(":")
    b = b or a
    col1, row1 = openpyxl.utils.cell.coordinate_from_string(a)
    col2, row2 = openpyxl.utils.cell.coordinate_from_string(b)
    c1, c2 = map(openpyxl.utils.column_index_from_string, (col1, col2))
    return [result.values.get(f"{openpyxl.utils.get_column_letter(c)}{r}", 0)
            for r in range(row1, row2 + 1) for c in range(c1, c2 + 1)]

def update_chart_xml(xml: str, result: Analysis) -> str:
    """各系列の参照と数値キャッシュを更新し、チャートの書式を保存する。"""
    # 標準OOXMLのc:プレフィックスを保持し、他のXML部品を再構築しない。
    def replace_reference(match):
        block = match.group(0)
        ref_match = re.search(r"<c:f>(.*?)</c:f>", block)
        if not ref_match:
            return block
        reference = ET.fromstring('<f>' + ref_match.group(1) + '</f>').text
        vals = range_values(reference, result)
        points = ''.join(f'<c:pt idx="{i}"><c:v>{float(v):.17g}</c:v></c:pt>' for i, v in enumerate(vals) if isinstance(v,(int,float)) and math.isfinite(v))
        cache = f'<c:numCache><c:formatCode>General</c:formatCode><c:ptCount val="{len(vals)}"/>{points}</c:numCache>'
        if '<c:numCache>' in block:
            return re.sub(r'<c:numCache>.*?</c:numCache>', lambda _: cache, block, flags=re.DOTALL)
        return block.replace('</c:numRef>', cache + '</c:numRef>')
    # 設定で近似範囲を変えた場合、同じ系列のX/Yを一緒に更新。
    def replace_series(match):
        block = match.group(0)
        # 測定系列の点は結ばず、Excelの近似線は独立して表示する。
        if re.search(r'<c:xVal>.*?!\$(?:O|AG)\$',block,re.DOTALL):
            block=re.sub(r'(<c:ser>\s*<c:idx\b[^>]*/>\s*<c:order\b[^>]*/>\s*(?:<c:tx>.*?</c:tx>\s*)?)(<c:spPr>.*?</c:spPr>)?',
                         lambda m:m[1]+'<c:spPr><a:ln><a:noFill/></a:ln></c:spPr>',block,count=1,flags=re.DOTALL)
            if '<c:marker>' in block:
                block=block.replace('<c:symbol val="none"/>','<c:symbol val="circle"/>')
            else:
                block=block.replace('<c:spPr><a:ln><a:noFill/></a:ln></c:spPr>',
                                    '<c:spPr><a:ln><a:noFill/></a:ln></c:spPr><c:marker><c:symbol val="circle"/><c:size val="5"/></c:marker>',1)
            def visible_trendline(match):
                trend=match.group(0)
                index=re.search(r'<c:idx val="(\d+)"',block)
                color=('246CAF','CE721B','398147')[int(index[1])%3 if index else 0]
                style=f'<c:spPr><a:ln w="28575"><a:solidFill><a:srgbClr val="{color}"/></a:solidFill><a:prstDash val="solid"/></a:ln></c:spPr>'
                return re.sub(r'^<c:trendline>(<c:spPr>.*?</c:spPr>)?',lambda _: '<c:trendline>'+style,trend,count=1,flags=re.DOTALL)
            block=re.sub(r'<c:trendline>.*?</c:trendline>',visible_trendline,block,flags=re.DOTALL)
        if not result.fits:
            return block
        for key in ("ion", "retarding", "saturation", "vp"):
            original = {"ion": (124, 128), "retarding": (134, 135),
                        "saturation": (144, 148), "vp": (136, 137)}[key]
            y_col = 'P' if key == 'ion' else 'AF'
            if ('<c:trendline>' in block and f'!$O${original[0]}:$O${original[1]}</c:f>' in block
                    and f'!${y_col}${original[0]}:${y_col}${original[1]}</c:f>' in block):
                lo, hi = result.fits[key].rows
                for col in ('O', y_col):
                    block = block.replace(f'!${col}${original[0]}:${col}${original[1]}', f'!${col}${lo}:${col}${hi}')
                if key=='ion':
                    positive=[v for v in result.voltage if v>0]
                    if positive:
                        forward=max(0.,max(positive)-max(result.voltage[(result.rows>=lo)&(result.rows<=hi)]))
                        def extend(match):
                            trend=re.sub(r'<c:forward\b[^>]*/>','',match.group(0))
                            return re.sub(r'(<c:trendlineType\b[^>]*/>)',lambda m:m[1]+f'<c:forward val="{forward:.17g}"/>',trend,count=1)
                        block=re.sub(r'<c:trendline>.*?</c:trendline>',extend,block,flags=re.DOTALL)
                break
        return block
    xml = re.sub(r'<c:ser>.*?</c:ser>', replace_series, xml, flags=re.DOTALL)
    if result.fits:
        xml = xml.replace('!$O$134</c:f>', '!'+openpyxl.utils.absolute_coordinate(result.vf_output_cell)+'</c:f>')
        xml = xml.replace('!$AF$134</c:f>', '!'+openpyxl.utils.absolute_coordinate(result.log_min_output_cell)+'</c:f>')
    positive_voltages=[result.values[f'O{r}'] for r in getattr(result,'all_rows',[]) if result.values[f'O{r}']>0]
    if positive_voltages:
        # テンプレートの入力表は保持し、既存Excelチャートも正電圧だけを表示。
        minimum=min(positive_voltages)*.95
        def positive_axis(match):
            axis=match.group(0)
            if not re.search(r'<c:axPos val="[bt]"',axis):return axis
            def scale(match):
                scaling=re.sub(r'<c:min\b[^>]*/>','',match.group(0))
                return scaling.replace('</c:scaling>',f'<c:min val="{minimum:.17g}"/></c:scaling>')
            return re.sub(r'<c:scaling>.*?</c:scaling>',scale,axis,count=1,flags=re.DOTALL)
        xml=re.sub(r'<c:valAx>.*?</c:valAx>',positive_axis,xml,flags=re.DOTALL)
    return re.sub(r'<c:numRef>.*?</c:numRef>', replace_reference, xml, flags=re.DOTALL)

def update_sheet_xml(xml: str, result: Analysis) -> str:
    """対象セルの更新を1回の走査で行う。"""
    pending = set(result.formulas) | set(result.text_updates)

    def replace(match):
        cell_xml = match.group(0)
        address = re.search(r'\br="([A-Z]+[0-9]+)"', cell_xml).group(1)
        if address not in pending:
            return cell_xml
        pending.remove(address)
        formula = result.formulas.get(address)
        preserve = bool(formula and formula == result.original_formulas.get(address))
        return update_cell_xml(cell_xml, address, result.values[address], formula, preserve)

    updated = re.sub(r'<c\b[^>]*?(?:/>|>.*?</c>)', replace, xml, flags=re.DOTALL)
    for address in sorted(pending & result.added_cells, key=openpyxl.utils.cell.coordinate_to_tuple):
        row_num, col_num = openpyxl.utils.cell.coordinate_to_tuple(address)
        new_cell = update_cell_xml(f'<c r="{address}"/>', address, result.values[address], result.formulas.get(address))
        row_pattern = re.compile(fr'<row\b(?=[^>]*\br="{row_num}")[^>]*>.*?</row>', re.DOTALL)
        def add(match):
            row_xml = match.group(0)
            for cell in re.finditer(r'<c\b[^>]*\br="([A-Z]+)[0-9]+"', row_xml):
                if openpyxl.utils.column_index_from_string(cell.group(1)) > col_num:
                    return row_xml[:cell.start()] + new_cell + row_xml[cell.start():]
            return row_xml.replace('</row>', new_cell+'</row>')
        updated, count = row_pattern.subn(add, updated)
        if count == 1:
            pending.remove(address)
        elif count == 0:
            row_xml = f'<row r="{row_num}">{new_cell}</row>'
            later_row = next((m for m in re.finditer(r'<row\b[^>]*\br="([0-9]+)"', updated)
                              if int(m.group(1)) > row_num), None)
            if later_row:
                updated = updated[:later_row.start()] + row_xml + updated[later_row.start():]
            else:
                updated = updated.replace('</sheetData>', row_xml+'</sheetData>')
            pending.remove(address)
    if pending:
        raise ValueError(f"出力テンプレートに必要なセルがありません: {sorted(pending)}")
    dimensions = re.search(r'<dimension ref="([A-Z]+[0-9]+)(?::([A-Z]+[0-9]+))?"', updated)
    if dimensions and result.added_cells:
        last = dimensions.group(2) or dimensions.group(1)
        old_row, old_col = openpyxl.utils.cell.coordinate_to_tuple(last)
        coordinates = [openpyxl.utils.cell.coordinate_to_tuple(c) for c in result.added_cells]
        last = f'{openpyxl.utils.get_column_letter(max(old_col, max(c for r,c in coordinates)))}{max(old_row,max(r for r,c in coordinates))}'
        updated = updated[:dimensions.start()] + re.sub(r'ref="[^"]+"', f'ref="{dimensions.group(1)}:{last}"', dimensions.group(0)) + updated[dimensions.end():]
    return updated

def write_workbook(source: Path, destination: Path, result: Analysis):
    """元ZIPの部品を保持し、計算セルとチャートキャッシュだけを更新。"""
    if source.resolve() == destination.resolve():
        raise ValueError("入力ファイルは上書きできません。別の出力先を指定してください。")
    with ZipFile(source) as zin:
        # 対象シートの実体パスをリレーションから解決。
        n = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
             "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
        book = ET.fromstring(zin.read('xl/workbook.xml'))
        sheet = next(s for s in book.findall('s:sheets/s:sheet', n) if s.attrib['name'] == result.sheet)
        rid = sheet.attrib['{' + n['r'] + '}id']
        rels = ET.fromstring(zin.read('xl/_rels/workbook.xml.rels'))
        target = next(r.attrib['Target'] for r in rels if r.attrib['Id'] == rid)
        sheet_path = target.lstrip('/') if target.startswith('/') else 'xl/' + target
        xml = zin.read(sheet_path).decode('utf-8')
        xml = update_sheet_xml(xml, result)
        temporary = destination.with_suffix('.tmp')
        try:
            with ZipFile(temporary, 'w', compression=ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    data = zin.read(item.filename)
                    if item.filename == sheet_path:
                        data = xml.encode('utf-8')
                    elif re.fullmatch(r'xl/charts/chart\d+\.xml', item.filename):
                        data = update_chart_xml(data.decode('utf-8'), result).encode('utf-8')
                    elif item.filename == 'xl/workbook.xml':
                        text = data.decode('utf-8')
                        def calc(match):
                            tag = re.sub(r'\s+(?:fullCalcOnLoad|forceFullCalc|calcOnSave)="[^"]*"', '', match.group(0))
                            return tag.replace('/>', ' fullCalcOnLoad="1" forceFullCalc="1" calcOnSave="1"/>')
                        data = re.sub(r'<calcPr\b[^>]*/>', calc, text).encode('utf-8')
                    zout.writestr(item, data)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

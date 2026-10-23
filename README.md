# 球状ラングミュアプローブのCSV解析

LP512のCSVを直接読み込み、Vf・Vp0・Vp・電子温度・電子飽和電流Ies・電子密度を求めます。
必要なのはPython 3.10以上、NumPy、Matplotlibです。元の測定CSVは変更しません。

## 準備と計算

```sh
python -m pip install -r requirements.txt
python lp_calculation.py
```

設定一覧から入力を選択します。条件が未指定の場合は、確認用のグラフと行番号表を保存して、Vf方法・電子減速領域・電子飽和領域をターミナルで入力します。`0`でキャンセルできます。
`--config 設定.json`は指定した設定、`--latest`は最新の設定済みデータを使用します。
`--no-export`を付けると計算結果の表示だけになり、出力・設定を保存しません。画面のない環境でも実行できます。

## CSVと行番号

必要な列名は`DAC_A_nominal_V`、`Vout_A_nominal_V`です。列順は問いません。
`resistance_A_ohm`は各測定点のA側抵抗[Ω]、`sweep`は掃引の識別に使います。UTF-8・Shift-JIS・BOM付きUTF-16と、カンマ・タブ・セミコロン区切りに対応します。

**行番号は、見出しと空行を除いた最初のデータを1行目として数えます。** `analysis_rows`と各近似範囲の開始・終了行は両端を含みます。CSVの先頭の測定点は、通常はファイル上の2行目ですが、指定値は`1`です。
保存済みのプロジェクト設定はこの方式へ移行済みです。同じ測定点を参照するよう範囲を換算しています。

| 設定JSONのキー | 意味 |
|---|---|
| `input` | 設定JSONからの相対パスで指定する入力CSV |
| `analysis_rows` | 解析する単一掃引のデータ行範囲 |
| `floating_potential_mode` | `minimum`：正電圧のln最小実測点、`lowest_two`：ln値が小さい2点の電圧平均 |
| `retarding_fit_rows` | 電子減速領域。Vf以上Vp0未満の正電圧点から指定 |
| `saturation_fit_rows` | 電子飽和領域。正電圧点から指定し、Vp0による下限制限はなし |
| `applicability_min_V` / `applicability_max_V` | Vin＋Voutの適用下限・上限[V]。標準−10 / +10、境界は含めない |
| `resistance_ohm` | A側抵抗列がない・空欄の場合の補完値[Ω]。標準1000000 |
| `probe_radius_m` | 球状プローブの半径[m]。標準0.02 |
| `output_dir` | 設定JSONからの相対パスで指定する保存先。省略時はoutputs内 |

## 計算式と手順

1. CSVから`Vout=-Vout_A_nominal_V`、`Ipol=-DAC_A_nominal_V/R`を求めます。
2. `Vin+Vout=Ipol*R+Vout`に対して、下限より大きく上限より小さい点を適用範囲とします。
3. 選択掃引内の負電圧点をすべて最小二乗近似し、イオン直線`a*Vout+b`を求め、`Ie=Ipol-(a*Vout+b)`で差し引きます。固定係数は使いません。
4. `ln(abs(Ie)/1 A)`を計算します。0 Aは未定義の空欄です。差し引きの丸め誤差は浮動小数点精度から判定し、実際に小さい電流に固定の下限を設けません。
5. 正電圧のln最小実測点、またはln値が小さい2点の電圧平均からVfを決めます。
6. ln変換前の補正Ieの隣接差分`abs(ΔIe)/abs(ΔVout)`が最大の区間の先頭電圧をVp0とします。隣接両点がVout>0の区間だけを候補とし、電圧差0は未定義の空欄です。測定点は削除・結合しません。
7. 指定した減速・飽和領域のln直線の交点をVpとし、正電圧のVp直前・直後の最寄り点から局所直線を自動算出します。Vpの両側に点がなければ外挿しません。
8. 局所直線にVpを代入し、`Ies=exp(局所直線の値)`[A]を求めます。
9. 減速直線の傾きaから`Te[K]=e/(k*a)`、`Te[eV]=1/a`。球面積`S=4*pi*r^2`、平均速度`v=sqrt(8*k*Te/(pi*me))`、`ne[m^-3]=4*Ies/(e*S*v)`。10^6で割ってcm^-3に換算します。

Rは`resistance_A_ohm`を測定点ごとに優先します。500 kΩなら手順3の変換は`-DAC_A/500000`、手順5は`Ipol*500000+Vout`です。B側抵抗は使いません。Rが0以下・非数値なら理由を表示して計算を止めます。
ここでeは電気素量[C]です。標準定数は`e=1.6e-19`、`k=1.38e-23 J/K`、`me=9.10939e-31 kg`です。

## 適用範囲を変える

```sh
python lp_calculation.py --config settings.json --applicability-min -8 --applicability-max 8 --configure
```

上下限だけを変更すると、同じ掃引と重なる連続した適用点へ解析範囲を更新します。範囲が変わる場合はVf方法・近似領域を選び直します。
`--analysis-rows 開始 終了`も指定した場合は、その行指定を優先して適用判定します。JSONで指定した行範囲は自動変更しません。
抵抗列がないCSVの補完値は`--resistance-ohm 500000`でも指定できます。

## 保存先とファイル

標準の保存先は`outputs/データ別フォルダ`です。`--output-dir フォルダ`で変更できます。

- `results_summary.csv`：先頭6行にVf・Vp0・減速ln直線の傾きΔIpol/ΔVout[1/V]・Vp・Ies・電子密度を、日本語名と単位付きで表示。物理量・近似係数・使用抵抗・上下限も保存します。
- `calculation_results.csv`：全結果と全測定点の変換・適用判定・補正電流・ln・隣接差分・各近似値を、category・data_row・item・value・unit付きで保存します。
- `analysis.csv`：解析対象点の計算過程。`iv_all.csv`：全測定点の変換・抵抗・適用判定。
- `summary.json`：条件・主要結果・近似係数・Vfの採用点。`analysis.png`：主要グラフの一覧。
- `selection_preview/preview.png`と`points.csv`：ターミナルで領域を選ぶための確認図・行番号表。

測定値は点だけ、近似式は直線で表示します。lnの縦軸は`ln(|Ie| / 1 A)`、電流の縦軸は`Ipol (A)`です。出力画像はPNGのみです。

| PNGファイル（graphs内） | 対応する手順・内容 |
|---|---|
| `01_raw_iv.png` | 手順4：変換後のIpol–Vout |
| `02_applicable_iv.png` | 手順7：適用判定内のIpol–Vout |
| `03_ion_fit.png` | 手順8–9：負電圧のイオン近似点と近似直線。負電圧から正電圧まで表示 |
| `04_corrected_current.png` | 手順10：イオン差し引き後の電流 |
| `05_log_current.png` | 手順12–14：ln表示とVf |
| `06_derivative.png` | 手順15：補正電流の隣接差分とVp0 |
| `07_retarding_fit.png` | 手順16–18：電子減速領域のln直線 |
| `08_saturation_fit.png` | 手順19：電子飽和領域のln直線 |
| `09_vp_intersection.png` | 手順20：減速・飽和直線の交点Vp |
| `10_vp_neighbors.png` | 手順21–22：Vp前後の自動近似とIes |

グラフ3以外はVout>0を表示します。Ln変換前の隣接差分[A/V]は、減速ln直線の傾き[1/V]とは別の量として記録します。

数式・変数・単位の日本語コメントは`lp_calculation.py`にあります。`calculate()`が計算の入口です。この1ファイルだけを測定CSV・設定JSONと一緒に別PCへ持ち運べます。Python本体とライブラリは移動先で導入してください。

## GUIで領域を選ぶ

```sh
python run_gui.py
```

既存プロジェクトでは`python run_project.py`、Windowsでは`run_analysis.cmd`でも起動できます。配布版は`run_gui.cmd`を使用できます。GUIにはTkinterを利用できるデスクトップ用Pythonが必要です。

1. CSVを`data`に入れると、GUIの一覧へ自動追加されます。ファイルごとに独立した設定を作ります。元CSVのコピーや変換はしません。
2. Vfの決定方法を選び、電子減速領域・電子飽和領域を横にドラッグします。開始・終了データ行の直接入力もできます。
3. 近似直線の交点Vpと、Vp前後の局所近似を即時表示します。局所評価領域のドラッグは不要です。
4. 「適用範囲：Vin＋Vout [V]」で下限・上限を入力し、「適用範囲を更新」で再計算します。適用点が変わった場合はVf・近似領域を選び直してください。
5. 「設定・全結果CSV・全グラフを保存」で保存します。表示中のズームも含めたGUIグラフを`graphs/11_gui_view.png`へ保存します。

GUIも計算専用版も同じ計算・CSV・PNG保存処理を使います。GUIで事前に保存しなくても、計算専用版は独立して実行できます。

VS Codeではこのフォルダを開き、使用するPythonを選択してください。既存プロジェクトのCtrl＋Shift実行は維持しています。更新前から開いているGUIは閉じて、再起動してください。

## 人工サンプルを直接指定する

この配布フォルダの設定を使う場合：

```sh
python lp_calculation.py --config settings.json
```

設定JSONを使わず、同梱の人工CSVを直接指定する場合（sample.csvまたはsynthetic.csv）：

```sh
python lp_calculation.py --input synthetic.csv --analysis-rows 1 53 --vf-mode minimum --retarding-rows 25 37 --saturation-rows 49 53
```

上の範囲は人工サンプル専用です。期待値はVp≈6 V、Te≈1.42857 eV、ne≈6319.31381 cm^-3です。
自分のデータではファイル名・Vf方法・行範囲を対象掃引に合わせて変更してください。`--configure`でターミナルから範囲を入力できます。
Windowsでは`run.cmd`、VS CodeではF5でも実行できます。macOS・Linuxでは必要に応じて`python3`を使用します。

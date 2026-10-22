# LP解析：球状Langmuirプローブ

LP512のCSV・Excelから、浮遊電位Vf、仮空間電位Vp0、空間電位Vp、電子温度Te、電子飽和電流Ies、電子密度neを計算します。**計算結果だけを表示する版**と、**グラフから領域を選ぶGUI版**を同梱しています。Excelアプリは不要です。

このリポジトリの`synthetic.csv`は数式から作った人工データです。実測データ、実際の解析結果、個人の設定ファイルは含みません。

## 計算結果だけを表示する

Python 3.10以上を用意し、このフォルダで実行します。

```sh
python -m pip install -r requirements.txt
python lp_calculation.py --config settings.json
```

Windowsでは準備後に`run.cmd`でも実行できます。macOS・Linuxでは環境に応じて`python3`を使ってください。コードはOS固有のパスを使いません。動作確認済みの環境はWindowsです。

標準では結果をターミナルに表示します。保存する場合は次を実行します。

```sh
python lp_calculation.py --config settings.json --output-dir results
```

`results/summary.json`に結果一覧、`results/analysis.csv`に電流補正・ln・差分の計算過程を保存します。入力と設定は上書きしません。

計算式・ファイル読込・実行処理は`lp_calculation.py`の1ファイルに統合しています。計算だけなら、このファイルと測定・設定を持ち運べます。CSVだけなら`numpy`、Excel入力には`openpyxl`も必要です。

## GUIで領域を選ぶ

GUI版はリポジトリ全体、または`LP_calculation_portable.zip`をダウンロードして使います。

```sh
python -m pip install -r requirements-gui.txt
python run_gui.py
```

Windowsでは準備後に`run_gui.cmd`でも開けます。Tkinterを利用できるデスクトップ用のPython環境が必要です。画面のない環境では計算専用版を使ってください。

1. 初回は人工サンプルを表示します。正電圧のlnグラフに、点と近似直線を表示します。点の横に行番号は出しません。
2. Vfの指定方法を選び、電子減速領域・電子飽和領域を横にドラッグします。開始・終了行の直接入力もできます。
3. 2本の近似直線の交点をVpとして即時表示し、Vp前後の点による局所近似式・直線も自動更新します。
4. 左側に電子温度・電子密度・各近似式を表示します。「設定を保存して解析結果を書き出す」で保存します。

自分のLP512形式のCSV・Excelは`data`フォルダに入れてください。サブフォルダも含めて自動検出し、画面上部の一覧へ追加します。新しいデータではVf方法・近似範囲を選んで保存してください。別ファイルの選択範囲を自動流用しません。

特定の設定から開く場合は、次を実行します。CSVの場合も自動で取込用Excelを作ります。

```sh
python run_gui.py --config settings.json
```

GUIの選択内容は`profiles`、結果は`outputs`へ保存します（明示した出力先がある場合はそちらを使用）。元の`settings.json`は変更しません。CSVの取込用Excelは元CSVと同じ場所の`.converted`に保存し、元CSVは上書きしません。

|出力|内容|
|---|---|
|`analysis_corrected.xlsx`|再計算用の数式・計算済み値。元Excelの書式・グラフがあれば保持|
|`analysis_report.xlsx`|結果一覧と解析段階ごとのグラフ|
|`analysis.csv` / `iv_all.csv`|補正・ln・差分の計算過程、全測定の適用判定|
|`analysis.png` / `analysis.svg`|グラフと結果一覧|
|`summary.json`|使用設定・各近似式・計算結果|

GUIで保存した設定を画面なしで再実行するときは、`lp_calculation.py --config profiles/対象.json`を使えます。

`LP_calculation_portable.zip`にはGUIに必要なファイルも含まれます。元PCの`.venv`はコピーせず、移動先で必要なライブラリを導入してください。

## 自分のデータを使う

測定CSVまたはExcelをローカルの`data`フォルダなどへ置き、`settings.json`を編集します。サンプルの範囲を実測データへそのまま流用しないでください。

|設定|内容|
|---|---|
|`input`|設定JSONのあるフォルダからの相対パス。例：`data/measurement.csv`|
|`sheet`|Excelのシート名。省略時は先頭シート。CSVでは無視|
|`analysis_rows`|適用範囲内の単一掃引の開始・終了行|
|`floating_potential_mode`|`minimum`：正電圧のln最小実測点、`lowest_two`：ln値が小さい2点の電圧平均|
|`retarding_fit_rows`|電子減速領域の開始・終了行。Vf以上Vp0未満|
|`saturation_fit_rows`|電子飽和領域の開始・終了行。正電圧から指定|
|`probe_radius_m`|球状プローブの半径[m]|

CSVには`DAC_A_nominal_V`と`Vout_A_nominal_V`の列が必要です。列順は自由で、UTF-8・Shift-JIS・UTF-16に対応します。解析行はヘッダー直後のデータを3行目と数え、空行を除きます。これは元Excelの解析列（O3=-H2）と同じ対応です。

ExcelはA1=`index`、F1=`DAC_A_nominal_V`、H1=`Vout_A_nominal_V`のLP512形式を使います。既存のGUIで保存した設定JSONも読み込めます。別PCへ移す際は`input`を相対パスへ変更してください。

## 計算の流れ

1. `Vout = -Vout_A_nominal_V`、`Ipol[A] = -DAC_A_nominal_V / 10^6`。
2. `-10 < Ipol×10^6 + Vout < 10`を満たす選択掃引を解析する。
3. 負電圧の全点を最小二乗近似し、`Ie = Ipol − (a_ion×Vout + b_ion)`を計算する。イオン係数は固定せず毎回求める。
4. `ln(abs(Ie))`を求める。差し引きの丸め誤差は計算精度に応じて0とし、0 Aのlnは空欄にする。
5. Vfを正電圧のln最小実測点、またはln値が小さい2点の電圧平均で決める。
6. lnにする前の補正電流で`abs(隣接ΔIe/隣接ΔV)`を計算し、最大区間の先頭点をVp0とする。
7. 指定した減速・飽和領域をln表示で線形近似し、2直線の交点をVpとする。飽和領域にはVp0による制限を設けない。
8. Vp前後の最寄り点を自動で局所近似し、`Ies = exp(局所直線のVp代入値)`。
9. `Te[K] = e/(k×減速傾き)`、球面積`S=4πr²`、平均速度`v=√(8kTe/(πme))`、`ne[m^-3]=4Ies/(eSv)`。`10^6`で割って`cm^-3`に換算する。

日本語コメント付きの計算本体は、`fit_line` → `correct_trace` → `calculate_parameters` → `calculate` → `main`の順に読むと追いやすくなっています。GUIと計算専用版は同じ数値計算を使います。イオン領域を手動指定する追加機能は含まない版です。

## 人工データとテスト

`synthetic.csv`はVoutが−5〜8 V、イオン直線が`4×10^-8 Vout − 1.5×10^-7 A`のデータです。電子電流は0 V未満で0、0〜6 Vで`exp(-18+0.7×Vout)`、6 V以上で`exp(-13.8+0.1×(Vout-6)) A`としています。物理モデルの妥当性を示す実験データではなく、コードの動作確認用です。

期待値はVf=0.25 V、Vp0=5.75 V、Vp=6 V、Te=約1.42857 eVです。

```sh
python -m unittest -v test_lp_calculation
```

GUIの動作確認は、画面を利用できるPython環境で次を実行します。

```sh
python -m unittest -v test_gui
```

実測データ、出力、ローカル設定、仮想環境は`.gitignore`で除外しています。実測CSV・Excelをコミットする場合は、内容を確認してください。

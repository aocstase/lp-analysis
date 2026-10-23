LP512点解析：更新機能のない旧版へ更新ツールを追加する方法

1. 解析GUIを閉じます。未保存の領域指定は先に保存してください。
2. このZIPの update_app.py と update.cmd を、使用中の lp_calculation.py と同じフォルダへ入れます。
3. update.cmd をダブルクリックします。
4. 「GitHubから最新版を確認」で版番号・更新対象を確認し、「更新を適用」を押します。
5. 完了したら「GUIを起動」を押します。以降はGUI上部の「アップデート」から更新できます。

data、profiles、outputs、設定JSON、CSV、.venvは残ります。
変更するプログラムは .updates/backups にバックアップします。
更新ツールの実行にはPython 3.10以上が必要です。追加ライブラリは不要です。
ネット接続できない場合は、最新版のリポジトリZIPまたはLP_calculation_portable.zipを別PCで取得し、「更新ZIPを選択」から適用してください。

GitHub: https://github.com/aocstase/lp-analysis

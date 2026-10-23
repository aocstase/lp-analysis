"""LP512解析の更新ツール。Python標準ライブラリだけで旧版のPCでも動く。"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

ROOT = Path(__file__).resolve().parent
APP_ID = 'lp512-analysis'
MANIFEST = 'update_manifest.json'
DOWNLOAD_URL = 'https://codeload.github.com/aocstase/lp-analysis/zip/refs/heads/main'
# 更新対象はプログラムと説明書だけ。CSV・設定JSON・data/profiles/outputs/.venvは含めない。
PROGRAM_FILES = (
    'lp_calculation.py', 'manual_analysis.py', 'analyze.py', 'data_catalog.py',
    'result_output.py', 'run_calculation.py', 'run_gui.py', 'run.cmd', 'run_gui.cmd',
    'update_app.py', 'update.cmd', 'README.md', 'requirements.txt', 'requirements-gui.txt', '.gitignore',
)
MAX_DOWNLOAD = 40 * 1024 * 1024
MAX_FILE = 2 * 1024 * 1024


class UpdateError(ValueError):
    """更新せずに利用者へ理由を表示するためのエラー。"""


@dataclass
class Package:
    version: str
    files: dict[str, bytes]
    manifest: bytes


def version_tuple(version):
    if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise UpdateError('版番号は1.1.0のような形式にしてください。')
    return tuple(map(int, version.split('.')))


def current_version(root):
    try:
        value = json.loads((Path(root) / MANIFEST).read_text(encoding='utf-8-sig'))['version']
        version_tuple(value)
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return '旧版（番号なし）'


def build_manifest(root, version):
    """配布側でコード変更後に実行し、更新対象のSHA-256を記録する。"""
    version_tuple(version)
    root = Path(root).resolve()
    payload = {'format': 1, 'app_id': APP_ID, 'version': version, 'python_min': [3, 10],
               'files': {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                         for name in PROGRAM_FILES}}
    path = root / MANIFEST
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return path


def read_package(payload):
    """ZIPを展開せず、指定ファイルの内容・ハッシュ・Python構文を検証する。"""
    if len(payload) > MAX_DOWNLOAD:
        raise UpdateError('更新ZIPが大きすぎます。')
    try:
        with ZipFile(io.BytesIO(payload)) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(name.casefold() for name in names)):
                raise UpdateError('ZIP内のファイル名が重複しています。')
            manifests = [name for name in names if PurePosixPath(name).name == MANIFEST]
            if len(manifests) != 1:
                raise UpdateError('更新情報のないZIPです。更新機能に対応した最新版のZIPを選んでください。')
            member = manifests[0]
            if '\\' in member or '..' in PurePosixPath(member).parts:
                raise UpdateError('ZIPのパスが不正です。')
            if archive.getinfo(member).file_size > 65536:
                raise UpdateError('更新情報が大きすぎます。')
            manifest = archive.read(member)
            info = json.loads(manifest.decode('utf-8-sig'))
            if not isinstance(info, dict) or info.get('format') != 1 or info.get('app_id') != APP_ID:
                raise UpdateError('LP512解析の更新ZIPではありません。')
            version = info.get('version')
            version_tuple(version)
            minimum = info.get('python_min')
            if (not isinstance(minimum, list) or len(minimum) != 2
                    or any(type(value) is not int for value in minimum)):
                raise UpdateError('更新情報のPython版指定が不正です。')
            if tuple(sys.version_info[:2]) < tuple(minimum):
                raise UpdateError(f'この版にはPython {minimum[0]}.{minimum[1]}以上が必要です。')
            hashes = info.get('files')
            if not isinstance(hashes, dict) or set(hashes) != set(PROGRAM_FILES):
                raise UpdateError('更新するプログラムの一覧が不正です。データ・設定には適用しません。')
            files = {}
            parent = PurePosixPath(member).parent
            for name, digest in hashes.items():
                if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
                    raise UpdateError(f'ハッシュ値が不正です: {name}')
                entry = archive.getinfo(str(parent / name))
                if entry.file_size > MAX_FILE or stat.S_ISLNK(entry.external_attr >> 16):
                    raise UpdateError(f'更新対象の形式・サイズが不正です: {name}')
                content = archive.read(entry)
                if hashlib.sha256(content).hexdigest() != digest:
                    raise UpdateError(f'ZIP内の内容が更新情報と一致しません: {name}')
                if name.endswith('.py'):
                    compile(content, name, 'exec')
                files[name] = content
            return Package(version, files, manifest)
    except (BadZipFile, KeyError, TypeError, UnicodeError, SyntaxError, RuntimeError, NotImplementedError) as error:
        raise UpdateError(f'更新ZIPを読み込めません: {error}') from error


def download_package():
    """公開GitHubのmainを取得。通信エラー時はローカルのプログラムを変更しない。"""
    request = Request(DOWNLOAD_URL, headers={'User-Agent': 'LP512-Updater'})
    try:
        with urlopen(request, timeout=30) as response:
            payload = response.read(MAX_DOWNLOAD + 1)
        return read_package(payload)
    except HTTPError as error:
        raise UpdateError(f'GitHubから取得できません（HTTP {error.code}）。ブラウザで取得した最新版ZIPも選択できます。') from error
    except (URLError, TimeoutError, OSError) as error:
        raise UpdateError('GitHubへ接続できません。接続を確認するか、取得済みの最新版ZIPを選択してください。') from error


def changed_files(root, package):
    root = Path(root).resolve()
    current = current_version(root)
    if current != '旧版（番号なし）' and version_tuple(package.version) < version_tuple(current):
        raise UpdateError(f'現在の{current}より古い{package.version}には更新しません。')
    return [name for name, payload in {**package.files, MANIFEST: package.manifest}.items()
            if not (root / name).is_file() or (root / name).read_bytes() != payload]


def apply_update(root, package):
    """全検証・バックアップ後に置換し、途中で失敗したら置換済みファイルを戻す。"""
    root = Path(root).resolve()
    if not (root / 'lp_calculation.py').is_file():
        raise UpdateError('更新先には、使用中のlp_calculation.pyがあるフォルダを指定してください。')
    changed = changed_files(root, package)
    if not changed:
        return None
    updates = root / '.updates'
    if updates.resolve() != updates:
        raise UpdateError('バックアップ先が解析フォルダの外を参照しています。')
    backup = updates / 'backups' / (datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid4().hex[:8])
    if not backup.resolve().is_relative_to(updates):
        raise UpdateError('バックアップ先のパスが不正です。')
    for name in changed:
        path = root / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise UpdateError(f'通常のファイルではありません: {name}')
    backup.mkdir(parents=True)
    original = [name for name in changed if (root / name).exists()]
    for name in original:
        shutil.copy2(root / name, backup / name)
    (backup / 'backup.json').write_text(json.dumps({
        'from_version': current_version(root), 'to_version': package.version,
        'files': original, 'new_files': [name for name in changed if name not in original]},
        ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    payloads = {**package.files, MANIFEST: package.manifest}
    installed = []
    with tempfile.TemporaryDirectory(prefix='staging_', dir=updates) as temporary:
        staging = Path(temporary)
        for name in changed:
            (staging / name).write_bytes(payloads[name])
        try:
            for name in changed:
                os.replace(staging / name, root / name)
                installed.append(name)
        except OSError as error:
            try:
                for name in reversed(installed):
                    if name in original:
                        shutil.copy2(backup / name, staging / name)
                        os.replace(staging / name, root / name)
                    else:
                        (root / name).unlink()
            except OSError as restore_error:
                raise UpdateError(f'復元できないファイルがあります。バックアップ: {backup}（{restore_error}）') from error
            raise UpdateError(f'更新できなかったため元に戻しました。バックアップ: {backup}（{error}）') from error
    return backup


def launch_gui(root):
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    return subprocess.Popen([sys.executable, str(Path(root) / 'run_gui.py')], cwd=root, **options)


def gui(root):
    """確認・ダウンロードだけ別スレッドで行い、適用は利用者の操作後に行う。"""
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    window = tk.Tk();window.title('LP512点解析 — アップデート')
    window.geometry('680x440');window.minsize(600,400)
    frame = ttk.Frame(window, padding=16);frame.pack(fill='both', expand=True)
    ttk.Label(frame, text=f'更新先: {root}', wraplength=630).pack(anchor='w')
    installed = tk.StringVar(value=f'現在のバージョン: {current_version(root)}')
    ttk.Label(frame, textvariable=installed).pack(anchor='w', pady=8)
    state = tk.StringVar(value='GitHubから確認するか、最新版のZIPを選択してください。')
    ttk.Label(frame, textvariable=state, wraplength=630).pack(anchor='w', pady=4)
    listing = tk.Text(frame, height=9, state='disabled');listing.pack(fill='both', expand=True, pady=8)
    ttk.Label(frame, text='data・profiles・outputs・設定JSON・CSV・.venvは更新しません。\n変更するプログラムは .updates/backups にバックアップします。').pack(anchor='w')
    controls = ttk.Frame(frame);controls.pack(fill='x', pady=(12,0))
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='lp-update')
    package = None
    closed = False

    def busy(value):
        check.configure(state='disabled' if value else 'normal')
        choose.configure(state='disabled' if value else 'normal')
        apply.configure(state='disabled' if value or package is None else 'normal')

    def show_list(lines):
        listing.configure(state='normal');listing.delete('1.0','end')
        listing.insert('1.0','\n'.join(lines));listing.configure(state='disabled')

    def load(loader):
        nonlocal package
        package = None;busy(True);state.set('更新データを確認しています…')
        show_list([])
        future = pool.submit(loader)
        def poll():
            nonlocal package
            if closed:return
            if not future.done():
                window.after(100,poll);return
            try:
                candidate = future.result();changes = changed_files(root,candidate)
                state.set(f'取得したバージョン: {candidate.version} ／ 更新対象: {len(changes)}ファイル')
                show_list(changes or ['すでに最新版です。'])
                package = candidate if changes else None
            except (OSError,ValueError) as error:
                state.set(str(error))
            busy(False)
        window.after(100,poll)

    def pick():
        path = filedialog.askopenfilename(parent=window,title='最新版のZIPを選択',filetypes=[('ZIP','*.zip')])
        if path:load(lambda:read_package(Path(path).read_bytes()))

    def install():
        nonlocal package
        if package is None:return
        if not messagebox.askyesno('更新を適用',f'バージョン {package.version} に更新しますか？\n解析GUIを閉じてから実行してください。\n\nデータ・解析設定・結果は残ります。',parent=window):return
        busy(True)
        try:
            backup = apply_update(root,package)
            installed.set(f'現在のバージョン: {current_version(root)}')
            state.set(f'更新完了。GUIを起動できます。バックアップ: {backup}')
            package = None
        except (OSError,ValueError) as error:
            state.set(str(error));messagebox.showerror('更新できませんでした',str(error),parent=window)
        busy(False)

    def close():
        nonlocal closed
        closed = True;pool.shutdown(wait=False,cancel_futures=True);window.destroy()

    check = ttk.Button(controls,text='GitHubから最新版を確認',command=lambda:load(download_package));check.pack(side='left')
    choose = ttk.Button(controls,text='更新ZIPを選択',command=pick);choose.pack(side='left',padx=6)
    apply = ttk.Button(controls,text='更新を適用',command=install,state='disabled');apply.pack(side='left')
    ttk.Button(controls,text='GUIを起動',command=lambda:launch_gui(root)).pack(side='right')
    window.protocol('WM_DELETE_WINDOW',close)
    window.mainloop()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target',type=Path,default=ROOT,help='使用中の解析フォルダ')
    parser.add_argument('--gui',action='store_true')
    parser.add_argument('--archive',type=Path,help='ネット接続を使わずZIPから更新')
    parser.add_argument('--check',action='store_true',help='確認だけで、書き換えない')
    parser.add_argument('--yes',action='store_true',help='確認後、そのまま更新を適用')
    parser.add_argument('--make-manifest',metavar='VERSION',help='配布側で更新情報を作成')
    args = parser.parse_args(argv)
    root = args.target.resolve()
    try:
        if args.make_manifest:
            print(build_manifest(root,args.make_manifest));return 0
        if args.gui:
            gui(root);return 0
        package = read_package(args.archive.read_bytes()) if args.archive else download_package()
        changes = changed_files(root,package)
        print(f'現在: {current_version(root)}\n取得した版: {package.version}')
        print('更新対象:\n' + '\n'.join(changes) if changes else 'すでに最新版です。')
        if args.check or not changes:return 0
        if not args.yes and (not sys.stdin.isatty() or input('解析GUIを閉じてください。更新しますか？ [y/N]: ').casefold()!='y'):
            print('更新を中止しました。適用する場合は--yesも指定できます。');return 0
        print(f'更新完了。バックアップ: {apply_update(root,package)}')
        return 0
    except (OSError,ValueError) as error:
        print(f'更新エラー: {error}',file=sys.stderr);return 1


if __name__ == '__main__':
    raise SystemExit(main())

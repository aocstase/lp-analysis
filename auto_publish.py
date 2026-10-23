"""開発用。保存後に変更が落ち着いたら、テスト済みのコードだけを自動公開する。"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from release_tools import ROOT, source_signature, publish_release, git_executable, ReleaseError


def status_path(root):return Path(root)/'.tools/auto_publish/status.json'


def write_status(root,state,message,**extra):
    path=status_path(root);path.parent.mkdir(parents=True,exist_ok=True)
    value={'state':state,'message':message,'pid':os.getpid(),
           'updated_at':datetime.now().isoformat(timespec='seconds'),**extra}
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    temporary.replace(path)
    print(f'[{value["updated_at"]}] {message}',flush=True)


def process_alive(pid):
    """Windowsではos.kill(pid,0)を使わず、終了状態の照会だけを行う。"""
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:return ctypes.get_last_error()==5
        try:
            code=wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle,ctypes.byref(code)) or code.value==259
        finally:kernel.CloseHandle(handle)
    try:os.kill(pid,0);return True
    except ProcessLookupError:return False
    except PermissionError:return True


def watch(root,settle=15):
    root=Path(root).resolve();folder=status_path(root).parent;folder.mkdir(parents=True,exist_ok=True)
    lock=folder/'watch.lock';stop=folder/'stop'
    if lock.exists():
        try:
            if not process_alive(int(lock.read_text(encoding='utf-8'))):lock.unlink()
        except (ValueError,OSError):pass
    try:handle=lock.open('x',encoding='utf-8')
    except FileExistsError:raise ReleaseError('自動反映は既に起動中です。状態は .tools/auto_publish/status.json で確認できます。')
    handle.write(str(os.getpid()));handle.close()
    stop.unlink(missing_ok=True)
    signature=None;changed_at=time.monotonic();retry_at=0;dirty=True
    try:
        write_status(root,'watching',f'自動反映を開始しました。保存後{settle:g}秒待ってテスト・公開します。')
        print('LP512_AUTO_PUBLISH_READY',flush=True)
        while not stop.exists():
            try:current=source_signature(root)
            except (OSError,ValueError):
                # エディタによる一時的なファイル置換中も監視は終了させない。
                time.sleep(1)
                continue
            now=time.monotonic()
            if current!=signature:
                signature=current;changed_at=now;dirty=True
            if dirty and now-changed_at>=settle and now>=retry_at:
                write_status(root,'checking','GitHub認証と変更内容を確認しています。')
                try:
                    result=subprocess.run([sys.executable,'-X','utf8',str(root/'release_tools.py'),
                                           '--root',str(root),'--publish'],cwd=root,capture_output=True,
                                          text=True,encoding='utf-8',errors='replace',timeout=420)
                except subprocess.TimeoutExpired:
                    write_status(root,'waiting','自動反映がタイムアウトしました。60秒後に再試行します。')
                    retry_at=time.monotonic()+60
                    continue
                if result.returncode:
                    write_status(root,'waiting',result.stderr[-4000:].strip() or '反映できませんでした。')
                    retry_at=time.monotonic()+60
                else:
                    write_status(root,'published','自動反映が完了しました。',result=result.stdout[-6000:])
                    dirty=source_signature(root)!=signature
                    signature=source_signature(root)
                    changed_at=time.monotonic()
            time.sleep(1)
        write_status(root,'stopped','自動反映を停止しました。')
    finally:
        lock.unlink(missing_ok=True);stop.unlink(missing_ok=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--settle',type=float,default=15)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--watch',action='store_true')
    mode.add_argument('--once',action='store_true')
    mode.add_argument('--login',action='store_true')
    mode.add_argument('--stop',action='store_true')
    args=parser.parse_args(argv);root=args.root.resolve()
    try:
        executable=git_executable()
        config=status_path(root).parent/'git.json';config.parent.mkdir(parents=True,exist_ok=True)
        config.write_text(json.dumps({'path':executable})+'\n',encoding='utf-8')
        if args.settle<1:raise ReleaseError('待機時間は1秒以上にしてください。')
        if args.login:
            # 認証はGit Credential Managerと本人のブラウザで行う。トークンをファイルに書かない。
            return subprocess.run([executable,'credential-manager','github','login',
                                   '--username','aocstase','--device','--no-ui']).returncode
        if args.stop:
            folder=status_path(root).parent;folder.mkdir(parents=True,exist_ok=True)
            (folder/'stop').touch();print('停止を要求しました。実行中の反映があれば完了後に停止します。');return 0
        if args.once:
            release=publish_release(root)
            write_status(root,'published',f'{release["version"]} の反映を確認しました。',commit=release['commit'])
        else:watch(root,args.settle)
        return 0
    except KeyboardInterrupt:return 0
    except (OSError,ValueError,subprocess.TimeoutExpired) as error:
        print(str(error),file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())

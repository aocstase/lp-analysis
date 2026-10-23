"""開発用。コードの同期・テスト・自動採番・更新情報・配布ZIPの作成をまとめる。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from update_app import PROGRAM_FILES, build_manifest, current_version, read_package, version_tuple

ROOT = Path(__file__).resolve().parent
STATE_FILE = 'release_sources.json'
DEVELOPER_FILES = ('release_tools.py', 'auto_publish.py', 'start_auto_publish.cmd', 'stop_auto_publish.cmd')
TEST_FILES = ('test_gui.py', 'test_lp_calculation.py', 'test_update.py', 'test_release.py')
# 人工サンプルだけを配布する。利用者のCSV・設定に置き換わっていたら送信しない。
FIXTURE_HASHES = {
    'settings.json': '5367e389b3aa286db3b7fd6d5512614e7b53e5e698ddba4b4f87f4c2180a73a6',
    'synthetic.csv': '55a0faf319448af6f54e7c1a6f6e00c2364fdbfa63ea5ade71e51def858fa034',
}
TEMPLATE_FILES = {'.gitignore', 'run.cmd', 'run_gui.cmd', 'requirements-gui.txt'}


class ReleaseError(ValueError):
    pass


def public_directory(root):
    root = Path(root).resolve()
    return root / 'github_lp_analysis' if (root / 'github_lp_analysis').is_dir() else root


def source_files(root):
    """配布してよいファイルを明示する。フォルダ丸ごとのコピー・git addはしない。"""
    root = Path(root).resolve();public = public_directory(root)
    files = {}
    for name in PROGRAM_FILES:
        files[name] = public / name if name in TEMPLATE_FILES or not (root / name).is_file() else root / name
    for name in ('README_UPDATE.txt', *DEVELOPER_FILES):
        files[name] = root / name if (root / name).is_file() else public / name
    for name in TEST_FILES:
        files[name] = root / 'tests' / name if (root / 'tests' / name).is_file() else public / name
    for name in FIXTURE_HASHES:files[name] = public / name
    missing = [name for name,path in files.items() if not path.is_file()]
    if missing:raise ReleaseError('配布に必要なファイルがありません: ' + ', '.join(missing))
    return files


def fingerprints(files):
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name,path in files.items()}


def source_signature(root):
    """保存監視はサイズ・時刻だけを調べ、変更がないファイルの本文を繰り返し読まない。"""
    return tuple((name, path.stat().st_size, path.stat().st_mtime_ns)
                 for name,path in source_files(root).items())


def stored_sources(public):
    try:
        state = json.loads((Path(public) / STATE_FILE).read_text(encoding='utf-8'))
        return state.get('sources', {})
    except (OSError, ValueError, AttributeError):return {}


def validate_release(candidate):
    result = subprocess.run([sys.executable, '-I', '-X', 'utf8', '-m', 'unittest',
                             'discover', '-s', str(candidate), '-p', 'test_*.py'],
                            cwd=candidate, capture_output=True, text=True, encoding='utf-8', timeout=240)
    if result.returncode:
        raise ReleaseError('テストに失敗したため公開しません。\n' + result.stdout[-4000:] + result.stderr[-6000:])
    return result.stderr.strip()


def make_zip(destination, files, prefix=''):
    """同じ内容なら同じZIPにし、時刻だけの変更で更新を繰り返さない。"""
    with ZipFile(destination, 'w', compression=ZIP_DEFLATED) as archive:
        for name,path in sorted(files.items()):
            info = ZipInfo(prefix + name, date_time=(2020,1,1,0,0,0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())


def release_files(public):
    names = [*PROGRAM_FILES, 'README_UPDATE.txt', *DEVELOPER_FILES, *TEST_FILES,
             *FIXTURE_HASHES, 'update_manifest.json', STATE_FILE,
             'LP_update_tool.zip', 'LP_calculation_portable.zip']
    return {name: Path(public) / name for name in names}


def prepare_release(root, *, minimum_version=None, validator=None):
    """一時フォルダで確認できた版だけ配布フォルダへ反映する。失敗時は旧版を維持。"""
    root = Path(root).resolve();public = public_directory(root)
    files = source_files(root);snapshot = fingerprints(files)
    for name,digest in FIXTURE_HASHES.items():
        if snapshot[name] != digest:
            raise ReleaseError(f'配布用の人工サンプルが変更されています: {name}。個人データは公開しません。')
    current = current_version(public)
    current = current if current != '旧版（番号なし）' else '1.0.0'
    baseline = max((current, minimum_version or current), key=version_tuple)
    if snapshot == stored_sources(public) and baseline == current:
        # 配布ファイルが手で変更された場合も、ハッシュを確認してから再利用する。
        manifest = json.loads((public / 'update_manifest.json').read_text(encoding='utf-8'))
        if all(hashlib.sha256((public/name).read_bytes()).hexdigest()==digest
               for name,digest in manifest['files'].items()):
            read_package((public/'LP_calculation_portable.zip').read_bytes())
            return {'version':current,'changed':False,'files':release_files(public),'tests':'検証済みの同一ソース'}
    major,minor,patch = version_tuple(baseline)
    version = f'{major}.{minor}.{patch+1}'
    tools = root / '.tools';tools.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='release_check_', dir=tools) as temporary:
        candidate = Path(temporary).resolve()
        if not candidate.is_relative_to(tools.resolve()):raise ReleaseError('検証先が不正です。')
        for name,path in files.items():shutil.copy2(path,candidate/name)
        for name in PROGRAM_FILES:
            if name.endswith('.py'):compile((candidate/name).read_bytes(),name,'exec')
        test_result = (validator or validate_release)(candidate)
        if fingerprints(source_files(root)) != snapshot:
            raise ReleaseError('テスト中にソースが変更されたため、公開せず次の保存を待ちます。')
        build_manifest(candidate,version)
        (candidate/STATE_FILE).write_text(json.dumps({'version':version,'sources':snapshot},
                                                   indent=2)+'\n',encoding='utf-8')
        make_zip(candidate/'LP_update_tool.zip',
                 {name:candidate/name for name in ('update_app.py','update.cmd','README_UPDATE.txt')})
        names = [*files, 'update_manifest.json', STATE_FILE, 'LP_update_tool.zip']
        make_zip(candidate/'LP_calculation_portable.zip', {name:candidate/name for name in names}, 'lp-analysis/')
        read_package((candidate/'LP_calculation_portable.zip').read_bytes())
        public.mkdir(exist_ok=True)
        for name,path in release_files(candidate).items():
            destination = public/name
            temporary_path = destination.with_name(destination.name+'.release-tmp')
            shutil.copy2(path,temporary_path);temporary_path.replace(destination)
    if public != root:
        for name in ('update_manifest.json','LP_update_tool.zip'):
            shutil.copy2(public/name,root/name)
    return {'version':version,'changed':True,'files':release_files(public),'tests':test_result}


def git_executable():
    """通常のPATHを優先し、このPCで確認済みのGitの場所も再利用できる。"""
    executable = shutil.which('git')
    if executable:return executable
    try:
        path = Path(json.loads((ROOT/'.tools/auto_publish/git.json').read_text(encoding='utf-8'))['path'])
        if path.is_file() and path.name.lower() in ('git.exe','git'):return str(path)
    except (OSError,ValueError,KeyError,TypeError):pass
    raise ReleaseError('開発PCにはGitが必要です。Git for Windowsを導入してください。')


def git_command(args, cwd, *, timeout=90):
    import os
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never')
    result = subprocess.run([git_executable(), *args], cwd=cwd, env=env, capture_output=True,
                            text=True, encoding='utf-8', errors='replace', timeout=timeout)
    if result.returncode:
        raise ReleaseError('GitHubへの自動反映に失敗しました。初回は --login で認証してください。\n'
                           + result.stderr[-3000:])
    return result.stdout.strip()


def publish_release(root, *, remote='https://github.com/aocstase/lp-analysis.git'):
    """新しい作業用cloneへ配布対象だけを追加し、通常のpushで一括反映。強制pushはしない。"""
    root = Path(root).resolve();tools = root/'.tools';tools.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='release_git_',dir=tools) as temporary:
        checkout = Path(temporary).resolve()/'repository'
        if not checkout.is_relative_to(tools.resolve()):raise ReleaseError('作業先が不正です。')
        git_command(['clone','--depth','1','--branch','main',remote,str(checkout)],root)
        # Windowsの改行自動変換で、作成したSHA-256とGitHubの内容がずれるのを防ぐ。
        git_command(['config','core.autocrlf','false'],checkout)
        # 認証がなければ、時間のかかるテスト・採番より先に止める。
        git_command(['push','--dry-run','origin','HEAD:main'],checkout)
        remote_version = current_version(checkout)
        release = prepare_release(root, minimum_version=None if remote_version=='旧版（番号なし）' else remote_version)
        for name,path in release['files'].items():shutil.copy2(path,checkout/name)
        git_command(['add','--',*release['files']],checkout)
        if not git_command(['diff','--cached','--name-only'],checkout):
            return release | {'published':False,'commit':git_command(['rev-parse','HEAD'],checkout)}
        author = git_command(['log','-1','--format=%an'],checkout)
        email = git_command(['log','-1','--format=%ae'],checkout)
        git_command(['-c',f'user.name={author}','-c',f'user.email={email}',
                     'commit','-m',f'Release {release["version"]}: automatic validated update'],checkout)
        git_command(['push','origin','HEAD:main'],checkout)
        return release | {'published':True,'commit':git_command(['rev-parse','HEAD'],checkout)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--publish',action='store_true',help='検証後にGitHubにも反映')
    args = parser.parse_args(argv)
    try:
        release = publish_release(args.root) if args.publish else prepare_release(args.root)
        print(json.dumps({key:value for key,value in release.items() if key!='files'},ensure_ascii=False,indent=2))
        return 0
    except (OSError,ValueError,subprocess.TimeoutExpired) as error:
        print(str(error),file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())

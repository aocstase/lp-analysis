"""自動配布の採番・検証・個人データ除外・ZIP整合性・失敗時の保持を確認する。"""
import json
import hashlib
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import release_tools as release
import update_app


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        (release.ROOT/'.tools').mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=release.ROOT/'.tools')
        self.root=Path(self.temp.name);self.public=self.root/'github_lp_analysis';self.public.mkdir()
        for name,path in release.source_files(release.ROOT).items():
            shutil.copy2(path,self.public/name)
        # 検証中の配布候補にはまだmanifestがないため、試験用の旧版を作る。
        update_app.build_manifest(self.public,'1.1.0')
        for name in ('lp_calculation.py','release_tools.py','auto_publish.py'):
            shutil.copy2(self.public/name,self.root/name)
        (self.root/'data').mkdir();(self.root/'data/private.csv').write_bytes(b'private measurement')
        (self.root/'settings.json').write_bytes(b'personal settings')
        self.baseline=update_app.current_version(self.public)

    def tearDown(self):self.temp.cleanup()

    def build(self,**kwargs):return release.prepare_release(self.root,validator=lambda path:'tests passed',**kwargs)

    def test_prepare_bumps_once_builds_valid_archives_and_ignores_private_data(self):
        result=self.build()
        a,b,c=update_app.version_tuple(self.baseline)
        self.assertEqual(result['version'],f'{a}.{b}.{c+1}')
        package=update_app.read_package((self.public/'LP_calculation_portable.zip').read_bytes())
        self.assertEqual(package.version,result['version'])
        with ZipFile(self.public/'LP_calculation_portable.zip') as archive:
            self.assertNotIn('lp-analysis/data/private.csv',archive.namelist())
            self.assertEqual(archive.read('lp-analysis/settings.json'),(self.public/'settings.json').read_bytes())
        self.assertEqual((self.root/'settings.json').read_bytes(),b'personal settings')
        self.assertEqual((self.root/'data/private.csv').read_bytes(),b'private measurement')
        self.assertFalse(self.build()['changed'])
        signature=release.source_signature(self.root)
        (self.root/'data/private.csv').write_bytes(b'changed measurement')
        self.assertEqual(release.source_signature(self.root),signature)
        self.assertFalse(self.build()['changed'])

    def test_source_change_increments_patch_and_updates_payload(self):
        first=self.build()
        path=self.root/'lp_calculation.py';path.write_bytes(path.read_bytes()+b'\n# new development\n')
        second=self.build()
        a,b,c=update_app.version_tuple(first['version'])
        self.assertEqual(second['version'],f'{a}.{b}.{c+1}')
        package=update_app.read_package((self.public/'LP_calculation_portable.zip').read_bytes())
        self.assertEqual(package.files['lp_calculation.py'],path.read_bytes())

    def test_failed_test_or_source_changed_during_test_keeps_previous_release(self):
        self.build()
        before=(self.public/'update_manifest.json').read_bytes()
        path=self.root/'lp_calculation.py';path.write_bytes(path.read_bytes()+b'\n# pending\n')
        def fail(candidate):raise release.ReleaseError('test failed')
        with self.assertRaisesRegex(release.ReleaseError,'test failed'):
            release.prepare_release(self.root,validator=fail)
        self.assertEqual((self.public/'update_manifest.json').read_bytes(),before)
        def modify(candidate):path.write_bytes(path.read_bytes()+b'\n# edited during validation\n')
        with self.assertRaisesRegex(release.ReleaseError,'ソースが変更'):
            release.prepare_release(self.root,validator=modify)
        self.assertEqual((self.public/'update_manifest.json').read_bytes(),before)

    def test_modified_public_fixture_is_never_packaged(self):
        (self.public/'synthetic.csv').write_bytes(b'private data replacing sample')
        with self.assertRaisesRegex(release.ReleaseError,'人工サンプル'):self.build()
        self.assertFalse((self.public/'LP_calculation_portable.zip').exists())

    def test_newer_remote_version_is_respected(self):
        self.assertEqual(self.build(minimum_version='9.4.7')['version'],'9.4.8')

    def test_zip_is_deterministic(self):
        source=self.root/'test.txt';source.write_bytes(b'content')
        release.make_zip(self.root/'a.zip',{'test.txt':source},'lp-analysis/')
        release.make_zip(self.root/'b.zip',{'test.txt':source},'lp-analysis/')
        self.assertEqual((self.root/'a.zip').read_bytes(),(self.root/'b.zip').read_bytes())

    def test_publish_to_local_repository_preserves_bytes_and_is_idempotent(self):
        # 実際のGit送信をローカルで再現し、改行と更新情報のハッシュも確認する。
        try:release.git_executable()
        except release.ReleaseError:self.skipTest('GitがないPCでは送信テストを省略')
        origin=self.root/'remote.git';seed=self.root/'seed'
        release.git_command(['init','--bare','--initial-branch=main',str(origin)],self.root)
        release.git_command(['clone',str(origin),str(seed)],self.root)
        shutil.copy2(self.public/'update_manifest.json',seed/'update_manifest.json')
        release.git_command(['add','update_manifest.json'],seed)
        release.git_command(['-c','user.name=Release test','-c','user.email=release@example.invalid',
                             'commit','-m','Initial fixture'],seed)
        release.git_command(['push','origin','main'],seed)
        with patch.object(release,'validate_release',return_value='verified test fixture'):
            first=release.publish_release(self.root,remote=str(origin))
            second=release.publish_release(self.root,remote=str(origin))
        self.assertTrue(first['published'])
        self.assertFalse(second['published'])
        self.assertEqual(first['commit'],second['commit'])
        manifest=json.loads(release.git_command(['--git-dir',str(origin),'show','main:update_manifest.json'],self.root))
        import subprocess
        for name,digest in manifest['files'].items():
            data=subprocess.check_output([release.git_executable(),'--git-dir',str(origin),'show',f'main:{name}'])
            self.assertEqual(hashlib.sha256(data).hexdigest(),digest,name)
        names=release.git_command(['--git-dir',str(origin),'ls-tree','-r','--name-only','main'],self.root)
        self.assertNotIn('private.csv',names)


if __name__=='__main__':unittest.main()

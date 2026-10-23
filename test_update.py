"""旧版からの更新、データ保持、破損ZIPの拒否、失敗時の復元を確認する。"""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError
from zipfile import ZipFile, ZIP_DEFLATED

import update_app as update


class UpdateTests(unittest.TestCase):
    def setUp(self):
        (update.ROOT/'.tools').mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=update.ROOT/'.tools')
        self.base=Path(self.temp.name)
        self.target=self.base/'old version';self.target.mkdir()
        (self.target/'lp_calculation.py').write_text('old = True\n',encoding='utf-8')
        self.protected={
            'data/試験/測定.csv':b'original data', 'outputs/試験/results_summary.csv':b'original results',
            'profiles/data_test.json':b'{"saved_selection":true}', 'settings.json':b'{"custom":true}',
            'analysis_config.json':b'custom settings', '.venv/keep.txt':b'Python environment',
            'synthetic.csv':b'custom sample', 'my_script.py':b'user = True\n'}
        for name,content in self.protected.items():
            path=self.target/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(content)
        self.contents={name:(b'release = "1.1.0"\n' if name.endswith('.py') else b'new program\n')
                       for name in update.PROGRAM_FILES}

    def tearDown(self):self.temp.cleanup()

    def archive(self,*,version='1.1.0',tamper=None,manifest_changes=None):
        manifest={'format':1,'app_id':update.APP_ID,'version':version,'python_min':[3,10],
                  'files':{name:hashlib.sha256(content).hexdigest() for name,content in self.contents.items()}}
        if manifest_changes:manifest.update(manifest_changes)
        data=io.BytesIO()
        with ZipFile(data,'w',compression=ZIP_DEFLATED) as archive:
            archive.writestr('lp-analysis-main/update_manifest.json',json.dumps(manifest))
            for name,content in self.contents.items():
                archive.writestr('lp-analysis-main/'+name,tamper if name=='lp_calculation.py' and tamper else content)
            archive.writestr('lp-analysis-main/data/evil.csv',b'do not install')
        return data.getvalue()

    def check_protected(self):
        for name,content in self.protected.items():
            self.assertEqual((self.target/name).read_bytes(),content,name)
        self.assertFalse((self.target/'data/evil.csv').exists())

    def test_update_old_version_preserves_inputs_settings_outputs_and_backs_up(self):
        old=(self.target/'lp_calculation.py').read_bytes()
        package=update.read_package(self.archive())
        self.assertEqual(update.current_version(self.target),'旧版（番号なし）')
        backup=update.apply_update(self.target,package)
        self.assertEqual((backup/'lp_calculation.py').read_bytes(),old)
        for name,content in self.contents.items():self.assertEqual((self.target/name).read_bytes(),content)
        self.assertEqual(update.current_version(self.target),'1.1.0')
        self.assertEqual(update.changed_files(self.target,package),[])
        self.assertIsNone(update.apply_update(self.target,package))
        self.check_protected()

    def test_failed_install_rolls_back_changed_files_and_removes_new_files(self):
        before=(self.target/'lp_calculation.py').read_bytes()
        original=os.replace;calls=[]
        def failing_replace(source,destination):
            calls.append(str(destination))
            if len(calls)==3:raise PermissionError('simulated file lock')
            return original(source,destination)
        with patch.object(update.os,'replace',side_effect=failing_replace):
            with self.assertRaisesRegex(update.UpdateError,'元に戻しました'):
                update.apply_update(self.target,update.read_package(self.archive()))
        self.assertEqual((self.target/'lp_calculation.py').read_bytes(),before)
        self.assertFalse((self.target/'manual_analysis.py').exists())
        self.assertFalse((self.target/update.MANIFEST).exists())
        self.check_protected()

    def test_bad_hash_protected_filename_invalid_app_or_python_are_rejected(self):
        attempts=[self.archive(tamper=b'broken = True'),
                  self.archive(manifest_changes={'files':{'../data/測定.csv':'0'*64}}),
                  self.archive(manifest_changes={'app_id':'another-application'}),
                  self.archive(manifest_changes={'python_min':[99,0]})]
        for payload in attempts:
            with self.subTest(payload_size=len(payload)):
                with self.assertRaises(ValueError):update.read_package(payload)
        self.assertFalse((self.target/'.updates').exists())
        self.check_protected()

    def test_downgrade_and_invalid_target_are_rejected(self):
        (self.target/update.MANIFEST).write_text('{"version":"2.0.0"}',encoding='utf-8')
        package=update.read_package(self.archive())
        with self.assertRaisesRegex(update.UpdateError,'古い'):update.apply_update(self.target,package)
        empty=self.base/'empty';empty.mkdir()
        with self.assertRaisesRegex(update.UpdateError,'lp_calculation.py'):update.apply_update(empty,package)
        self.assertEqual(list(empty.iterdir()),[])

    def test_manifest_generation_hashes_only_program_files(self):
        release=self.base/'release';release.mkdir()
        for name,content in self.contents.items():(release/name).write_bytes(content)
        path=update.build_manifest(release,'1.2.0')
        manifest=json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(manifest['version'],'1.2.0')
        self.assertEqual(set(manifest['files']),set(update.PROGRAM_FILES))

    def test_cli_check_is_read_only_and_apply_works_without_analysis_dependencies(self):
        archive=self.base/'update.zip';archive.write_bytes(self.archive())
        command=[sys.executable,'-I','-X','utf8',str(update.ROOT/'update_app.py'),
                 '--target',str(self.target),'--archive',str(archive)]
        checked=subprocess.run([*command,'--check'],capture_output=True,text=True,encoding='utf-8',timeout=15)
        self.assertEqual(checked.returncode,0,checked.stderr)
        self.assertIn('1.1.0',checked.stdout)
        self.assertFalse((self.target/'.updates').exists())
        applied=subprocess.run([*command,'--yes'],capture_output=True,text=True,encoding='utf-8',timeout=15)
        self.assertEqual(applied.returncode,0,applied.stderr)
        self.check_protected()

    def test_network_failure_returns_clear_message_and_does_not_change_files(self):
        with patch.object(update,'urlopen',side_effect=URLError('offline')):
            with self.assertRaisesRegex(update.UpdateError,'接続できません'):update.download_package()
        self.assertFalse((self.target/'.updates').exists())
        self.check_protected()


if __name__=='__main__':unittest.main()

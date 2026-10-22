"""人工CSVだけで、GUI起動・領域選択・保存とファイル追加を確認する。"""
import json
from pathlib import Path
import shutil
import tempfile
import tkinter as tk
import unittest

import openpyxl

from lp_calculation import calculate, load_config
from manual_analysis import ManualAnalysisApp
from run_gui import prepare_gui_profile

ROOT = Path(__file__).resolve().parent


class GuiTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.tools').mkdir(exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(dir=ROOT / '.tools')
        self.base = Path(self.folder.name)
        for name in ('synthetic.csv', 'settings.json'):
            shutil.copy2(ROOT / name, self.base / name)
        self.config_path = self.base / 'settings.json'
        self.profile = prepare_gui_profile(self.config_path, root=self.base)
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = ManualAnalysisApp(self.root, self.profile, data_dir=self.base / 'data', profile_dir=self.base / 'profiles')
        self.root.update_idletasks()

    def tearDown(self):
        self.app.close()
        self.folder.cleanup()

    def test_sample_matches_cli_drag_updates_vp_and_export(self):
        config = load_config(self.config_path)
        expected = calculate(self.base / 'synthetic.csv', config)
        self.assertEqual(self.app.result.metrics, expected.metrics)
        self.assertFalse(self.app.ax.texts)
        self.assertTrue(all(x > 0 for x in self.app.ax.lines[0].get_xdata()))
        self.app.vars['vf_mode'].set('unselected')
        self.app.mode.set('retarding')
        self.app.on_span(.75 - 1e-6, 3.75 + 1e-6)
        self.assertIsNone(self.app.result)
        self.assertAlmostEqual(self.app.geometry_vp, 6., places=12)
        self.assertIn('vp', self.app.geometry_fits)
        self.assertTrue(any('Retarding fit:' in line.get_label() for line in self.app.ax.lines))
        self.app.vars['vf_mode'].set('minimum')
        self.assertTrue(self.app.save_and_export())
        saved = json.loads(self.profile.read_text(encoding='utf-8'))
        self.assertEqual(saved['retarding_fit_rows'], [26, 38])
        out = (self.profile.parent / saved['output_dir']).resolve()
        for name in ('analysis_corrected.xlsx', 'analysis_report.xlsx', 'analysis.csv', 'analysis.png', 'analysis.svg', 'summary.json'):
            self.assertTrue((out / name).exists(), name)
        book = openpyxl.load_workbook(out / 'analysis_corrected.xlsx', data_only=True)
        try:self.assertAlmostEqual(book.active['AQ128'].value, self.app.result.metrics['electron_density_cm3'])
        finally:book.close()
        self.assertEqual(load_config(self.config_path), config)
        self.assertEqual(prepare_gui_profile(self.config_path, root=self.base), self.profile)
        self.assertEqual(json.loads(self.profile.read_text(encoding='utf-8'))['retarding_fit_rows'], [26, 38])

    def test_csv_drop_is_detected_and_has_separate_unspecified_ranges(self):
        data = self.base / 'data'
        data.mkdir()
        shutil.copy2(self.base / 'synthetic.csv', data / '追加した測定.csv')
        self.app.refresh_catalog()
        label = next(name for name in self.app.profiles if '追加した測定.csv' in name)
        self.assertTrue(self.app.load_profile(self.app.profiles[label]))
        self.assertIsNone(self.app.result)
        self.assertEqual(self.app.vars['vf_mode'].get(), 'unselected')
        self.assertEqual(self.app.vars['retarding_start'].get(), '')
        self.app.vars['vf_mode'].set('minimum')
        self.app.mode.set('retarding');self.app.on_span(1 - 1e-6, 4 + 1e-6)
        self.app.mode.set('saturation');self.app.on_span(7 - 1e-6, 8 + 1e-6)
        self.assertIsNotNone(self.app.result)
        self.assertAlmostEqual(self.app.result.metrics['plasma_potential_V'], 6., places=12)


if __name__ == '__main__':
    unittest.main()

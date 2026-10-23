"""CSVだけでGUI・ドラッグ・適用範囲・保存・ファイル追加を確認する。"""
import json
from pathlib import Path
import shutil
import tempfile
import tkinter as tk
import unittest

import lp_calculation as lp
from manual_analysis import ManualAnalysisApp
from run_gui import prepare_gui_profile


class GuiTests(unittest.TestCase):
    def setUp(self):
        (lp.ROOT/'.tools').mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=lp.ROOT/'.tools');self.base=Path(self.temp.name)
        sample=lp.ROOT/'synthetic.csv'
        if not sample.exists():sample=lp.ROOT/'LP_standalone/sample.csv'
        shutil.copy2(sample,self.base/'sample.csv')
        config={'input':'sample.csv','analysis_rows':[1,53],'floating_potential_mode':'minimum',
                'retarding_fit_rows':[25,37],'saturation_fit_rows':[49,53]}
        self.settings=self.base/'settings.json';self.settings.write_text(json.dumps(config),encoding='utf-8')
        self.profile=prepare_gui_profile(self.settings,root=self.base)
        self.root=tk.Tk();self.root.withdraw()
        self.app=ManualAnalysisApp(self.root,self.profile,data_dir=self.base/'data',profile_dir=self.base/'profiles')
        self.root.update_idletasks()

    def tearDown(self):self.app.close();self.temp.cleanup()

    def test_same_results_as_terminal_and_immediate_drag_intersection(self):
        expected=lp.calculate(self.base/'sample.csv',lp.load_config(self.settings))
        self.assertEqual(self.app.result.metrics,expected.metrics)
        self.assertEqual(self.app.rows[[0,-1]].tolist(),[1,53])
        self.assertFalse(self.app.ax.texts)
        self.app.vars['vf_mode'].set('unselected')
        self.app.mode.set('retarding');self.app.on_span(.75-1e-6,3.75+1e-6)
        self.assertIsNone(self.app.result)
        self.assertAlmostEqual(self.app.geometry_vp,6.,places=12)
        self.assertIn('vp',self.app.geometry_fits)
        self.assertTrue(any('Vp neighbors fit:' in line.get_label() for line in self.app.ax.lines))

    def test_save_keeps_zoom_and_original_settings_and_writes_current_view(self):
        before=self.settings.read_bytes()
        self.app.ax.set_xlim(1.2,7.2);self.app.ax.set_ylim(-18,-12)
        self.assertTrue(self.app.save_and_export())
        self.assertEqual(self.app.ax.get_xlim(),(1.2,7.2))
        config=lp.load_config(self.profile);out=(self.profile.parent/config['output_dir']).resolve()
        self.assertEqual(len(list((out/'graphs').glob('*.png'))),11)
        self.assertTrue((out/'graphs/11_gui_view.png').exists())
        self.assertEqual({p.suffix for p in out.rglob('*') if p.is_file()},{'.csv','.png','.json'})
        self.assertEqual(self.settings.read_bytes(),before)
        self.assertEqual(prepare_gui_profile(self.settings,root=self.base),self.profile)
        self.assertEqual(list(self.base.glob('*.csv')),[self.base/'sample.csv'])

    def test_bound_controls_reload_and_reset_choices(self):
        self.app.vars['applicability_min_V'].set('-4')
        self.app.vars['applicability_max_V'].set('7')
        self.app.refresh()
        self.assertNotEqual(self.app.base['analysis_rows'],[1,53])
        self.assertIsNone(self.app.result)
        self.assertEqual(self.app.vars['vf_mode'].get(),'unselected')
        self.assertEqual(self.app.vars['retarding_start'].get(),'')
        self.app.vars['applicability_min_V'].set('8');self.app.refresh()
        self.assertIn('下限 < 上限',self.app.status.get())

    def test_csv_drop_is_auto_registered_without_converting_input(self):
        data=self.base/'data';data.mkdir();added=data/'追加測定.csv';shutil.copy2(self.base/'sample.csv',added)
        self.app.data_dir=data;self.app.refresh_catalog()
        self.assertTrue(self.app.load_profile(self.app.profiles[added.name]))
        self.assertIsNone(self.app.result)
        self.assertEqual(self.app.vars['retarding_start'].get(),'')
        self.app.vars['vf_mode'].set('minimum')
        self.app.mode.set('retarding');self.app.on_span(1-1e-6,4+1e-6)
        self.app.mode.set('saturation');self.app.on_span(7-1e-6,8+1e-6)
        self.assertIsNotNone(self.app.result)
        self.assertAlmostEqual(self.app.result.metrics['plasma_potential_V'],6.,places=12)
        self.assertEqual(list(data.iterdir()),[added])


if __name__=='__main__':unittest.main()

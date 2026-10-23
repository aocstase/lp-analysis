"""CSVだけでGUI・ドラッグ・適用範囲・保存・ファイル追加を確認する。"""
import json
from pathlib import Path
import shutil
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch
from threading import get_ident

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

    def test_selector_and_drag_reuse_cache(self):
        with patch.object(Path,'read_text',side_effect=AssertionError('一覧の設定を再読込した')):
            self.app.update_selector(self.app.config_path)
        with patch.object(Path,'read_bytes',side_effect=AssertionError('ドラッグ中にCSVを再読込した')):
            self.app.mode.set('retarding');self.app.on_span(.75-1e-6,3.75+1e-6)
            self.assertIsNotNone(self.app.result)
            self.assertAlmostEqual(self.app.result.metrics['plasma_potential_V'],6.,places=12)

    def test_update_tool_opens_only_after_confirmation_and_closes_analysis_gui(self):
        with patch('manual_analysis.messagebox.askyesno',return_value=False), \
             patch('manual_analysis.subprocess.Popen') as launch:
            self.app.open_update_tool();launch.assert_not_called()
        with patch('manual_analysis.messagebox.askyesno',return_value=True), \
             patch('manual_analysis.subprocess.Popen') as launch,patch.object(self.app,'close') as close:
            self.app.open_update_tool()
            self.assertIn(str(lp.ROOT/'update_app.py'),launch.call_args.args[0])
            close.assert_called_once()

    def test_directory_watch_runs_off_gui_thread(self):
        main_thread=get_ident();threads=[]
        def scan(directory):
            threads.append(get_ident())
            return self.app._snapshot
        self.root.after_cancel(self.app._watch_id)
        with patch('manual_analysis.data_snapshot',side_effect=scan):
            self.app.watch_data()
            future=self.app._snapshot_future
            if future is not None:
                future.result(timeout=5)
                self.root.after_cancel(self.app._watch_id)
                self.app.watch_data()
        self.assertEqual(len(threads),1)
        self.assertNotEqual(threads[0],main_thread)

    def test_explicit_list_refresh_rechecks_saved_status(self):
        with patch('manual_analysis.saved_output_directory',return_value=None) as check:
            self.app.update_selector(self.app.config_path)
            self.assertEqual(check.call_count,0)
            self.app.refresh_catalog()
            self.assertGreater(check.call_count,0)

    def show_stage(self,index):
        self.app.tabs.select(self.app.stage_page)
        self.app.stage_tabs.select(index)
        self.root.update()
        self.app.update_stage_graph()
        return self.app.stage_views[index]['ax']

    def test_stage_tabs_are_lazy_and_show_all_six_without_region_selection(self):
        self.assertEqual(self.app.stage_views,{})
        self.app.vars['vf_mode'].set('unselected')
        for key in ('retarding_start','retarding_end','saturation_start','saturation_end'):
            self.app.vars[key].set('')
        self.app.refresh();self.assertIsNone(self.app.result)
        for index in range(6):
            ax=self.show_stage(index)
            self.assertTrue(ax.lines)
            if index==2:
                self.assertLess(ax.get_xlim()[0],0)
                self.assertTrue(any(line.get_label().startswith('Iion=') for line in ax.lines))
            else:self.assertEqual(ax.get_xlim()[0],0)
        self.assertEqual(len(self.app.stage_views),6)
        self.assertEqual(self.app.stage_views[4]['ax'].get_ylabel(),'ln(|Ie| / 1 A)')
        self.assertEqual(self.app.stage_views[5]['ax'].get_ylabel(),'abs(delta Ie/delta V) (A/V)')
        self.assertTrue(any(line.get_label().startswith('Vp0=') for line in self.app.stage_views[5]['ax'].lines))

    def test_stage_cache_keeps_zoom_and_updates_vf_or_source(self):
        ax=self.show_stage(4);ax.set_xlim(1,7)
        with patch('manual_analysis.draw_preprocessing_stage',side_effect=AssertionError('不要な再描画')):
            self.app.update_stage_graph()
            self.assertEqual(ax.get_xlim(),(1.,7.))
        self.app.vars['vf_mode'].set('lowest_two');self.app.refresh()
        self.assertTrue(any(line.get_label().startswith('Vf=') for line in ax.lines))
        other=self.base/'data'/'別試験.csv';other.parent.mkdir()
        shutil.copy2(self.base/'sample.csv',other)
        self.app.refresh_catalog()
        self.assertTrue(self.app.load_profile(self.app.profiles[other.name]))
        self.assertEqual(self.app.stage_views[4]['key'][0],str(other))
        self.assertFalse(any(line.get_label().startswith('Vf=') for line in ax.lines))

    def test_stage_export_saves_one_or_six_without_changing_selection_or_zoom(self):
        ax=self.show_stage(5);ax.set_xlim(1,7)
        selected=self.app.stage_tabs.select()
        self.assertTrue(self.app.save_stage_graphs())
        out=lp.output_directory(self.app.source,self.app.base,self.app.config_path,root=self.base)/'graphs'
        self.assertEqual([path.name for path in out.iterdir()],['06_derivative.png'])
        self.assertEqual(ax.get_xlim(),(1.,7.))
        views=set(self.app.stage_views)
        self.assertTrue(self.app.save_stage_graphs(all_stages=True))
        self.assertEqual({path.name for path in out.iterdir()},
                         {name+'.png' for name,_ in lp.PREPROCESSING_STAGES})
        self.assertEqual(self.app.stage_tabs.select(),selected)
        self.assertEqual(set(self.app.stage_views),views)
        self.assertEqual(ax.get_xlim(),(1.,7.))
        for path in out.iterdir():
            self.assertTrue(path.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertFalse((out.parent/'results_summary.csv').exists())
        self.assertTrue(self.app.profile.get().startswith('【未保存】'))

    def test_all_six_export_works_before_vf_and_region_selection(self):
        self.app.vars['vf_mode'].set('unselected')
        for key in ('retarding_start','retarding_end','saturation_start','saturation_end'):
            self.app.vars[key].set('')
        self.app.refresh();self.assertIsNone(self.app.result)
        self.show_stage(0)
        self.assertTrue(self.app.save_stage_graphs(all_stages=True))
        out=lp.output_directory(self.app.source,self.app.base,self.app.config_path,root=self.base)
        self.assertEqual(len(list((out/'graphs').glob('*.png'))),6)

    def test_overview_displays_six_graphs_and_exports_one_combined_png(self):
        self.show_stage(6)
        view=self.app.stage_views[6]
        self.assertEqual(len(view['figure'].axes),6)
        self.assertTrue(all(ax.lines for ax in view['axes']))
        self.assertEqual(view['axes'][4].get_ylabel(),'ln(|Ie| / 1 A)')
        with patch('manual_analysis.draw_preprocessing_stage',side_effect=AssertionError('不要な再描画')):
            self.app.update_stage_graph()
        self.assertTrue(self.app.save_stage_graphs())
        out=lp.output_directory(self.app.source,self.app.base,self.app.config_path,root=self.base)/'graphs'
        self.assertEqual([path.name for path in out.iterdir()],['01-06_overview.png'])

    def test_save_keeps_zoom_and_original_settings_and_writes_current_view(self):
        before=self.settings.read_bytes()
        self.assertTrue(self.app.profile.get().startswith('【未保存】'))
        self.app.ax.set_xlim(1.2,7.2);self.app.ax.set_ylim(-18,-12)
        self.assertTrue(self.app.save_and_export())
        self.assertTrue(self.app.profile.get().startswith('【保存済み】'))
        self.app.refresh_catalog()
        self.assertTrue(self.app.profile.get().startswith('【保存済み】'))
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

    def test_nested_csv_uses_mirrored_output_and_save_indicator_tracks_each_file(self):
        folder=self.base/'data'/'試験A'/'測定フォルダ';folder.mkdir(parents=True)
        for name in ('測定1.csv','測定2.csv'):
            shutil.copy2(self.base/'sample.csv',folder/name)
        self.app.refresh_catalog()
        label='試験A/測定フォルダ/測定1.csv'
        self.assertTrue(self.app.load_profile(self.app.profiles[label]))
        self.app.vars['vf_mode'].set('minimum')
        self.app.mode.set('retarding');self.app.on_span(.75-1e-6,3.75+1e-6)
        self.app.mode.set('saturation');self.app.on_span(7-1e-6,8+1e-6)
        self.assertTrue(self.app.save_and_export())
        out=self.base/'outputs'/'試験A'/'測定フォルダ'/'測定1'
        self.assertTrue((out/'results_summary.csv').is_file())
        self.assertIn('【保存済み】 測定1.csv（試験A/測定フォルダ）',self.app.selector['values'])
        other='試験A/測定フォルダ/測定2.csv'
        self.assertIn('【未保存】 測定2.csv（試験A/測定フォルダ）',self.app.selector['values'])
        self.app.profile.set('【未保存】 測定2.csv（試験A/測定フォルダ）')
        self.app.selector.event_generate('<<ComboboxSelected>>')
        self.root.update()
        self.assertEqual(self.app.source,folder/'測定2.csv')
        self.assertFalse((self.base/'outputs'/'試験A'/'測定フォルダ'/'測定2').exists())


if __name__=='__main__':unittest.main()

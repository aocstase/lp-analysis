"""ln|Ie|–Voutを見ながらVf・近似領域を利用者が選択する画面。"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import math
import os
import subprocess
import sys
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

import numpy as np
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.widgets import SpanSelector

from analyze import analyze, export_analysis
from lp_calculation import (ROOT, read_trace, fit_line, intersect_fits, fit_vp_neighbors,
                            normalize_config, minimum_floating_potential, clear_measurement_cache)
from data_catalog import DEFAULT_CONFIGS, data_snapshot, discover_profiles, saved_output_directory
from lp_calculation import output_directory, applicability_bounds, select_applicable_rows
from lp_calculation import read_points, draw_preprocessing_stage, PREPROCESSING_STAGES

COLORS = {'retarding':'#246caf','saturation':'#ce721b','vp':'#398147'}
REGION_KEYS=('retarding','saturation')
STAGE_LABELS=('1 生データ','2 適用範囲','3 イオン近似',
              '4 イオン差し引き後','5 ln表示・Vf','6 隣接差分・Vp0','6枚一覧')


def selected_config(base, entries):
    """フォームの明示的な指定だけから設定を作り、選択範囲を自動変更しない。"""
    config=normalize_config(base | {key: entries.get(key, base.get(key, default))
                                   for key, default in [('applicability_min_V', -10), ('applicability_max_V', 10)]})
    first,last=config['analysis_rows']
    mode=entries['vf_mode']
    if mode not in ('minimum','lowest_two'):
        raise ValueError('Vfを目視で確認して「最小の実測点」か「小さい2点の平均」を選んでください。')
    config['floating_potential_mode']=mode
    for key,label in [('retarding','電子減速領域'),('saturation','電子飽和領域')]:
        try:
            lo,hi=int(entries[key+'_start']),int(entries[key+'_end'])
        except ValueError as error:
            raise ValueError(f'{label}の開始・終了行を指定してください。') from error
        if not first<=lo<hi<=last:
            raise ValueError(f'{label}は{first}〜{last}行の中で2点以上選択してください。')
        config[key+'_fit_rows']=[lo,hi]
    return config


def save_config(path, config):
    temporary=path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(config,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    temporary.replace(path)


class ManualAnalysisApp:
    def __init__(self, root, config_path=None, *, data_dir=None, profile_dir=None):
        self.root=root
        self.data_dir=Path(data_dir or ROOT/'data').resolve()
        self.profile_dir=Path(profile_dir or ROOT/'profiles').resolve()
        self.initial_config=Path(config_path or ROOT/'analysis_config.json').resolve()
        self.profiles={};self.selection_profiles={};self.catalog_errors=[];self._snapshot=None
        self._selector_cache={}
        self._watch_pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='lp-data-watch')
        self._snapshot_future=None
        self._watch_id=None;self._closed=False;self.has_trace=False
        root.title('LP512点解析')
        root.geometry('1360x860');root.minsize(1050,700)
        root.protocol('WM_DELETE_WINDOW',self.close)
        root.columnconfigure(0,weight=1);root.rowconfigure(2,weight=1)
        self.result=None;self.loading=False
        self.geometry_fits={};self.geometry_vp=None;self.geometry_error=''
        self.profile=tk.StringVar(value='')
        self.mode=tk.StringVar(value='retarding')
        self.vars={key:tk.StringVar() for key in ['vf_mode',
                   'retarding_start','retarding_end','saturation_start','saturation_end',
                   'applicability_min_V','applicability_max_V']}
        header=ttk.Frame(root,padding=(10,8));header.grid(row=0,column=0,sticky='ew')
        ttk.Label(header,text='解析するデータ').pack(side='left')
        self.selector=ttk.Combobox(header,textvariable=self.profile,values=[],state='readonly',width=65,
                                 postcommand=lambda:self.update_selector(getattr(self,'config_path',None)))
        self.selector.pack(side='left',padx=8)
        self.selector.bind('<<ComboboxSelected>>',lambda event:self.load_profile(self.selection_profiles[self.profile.get()]))
        ttk.Button(header,text='一覧更新',command=self.refresh_catalog).pack(side='left')
        ttk.Button(header,text='アップデート',command=self.open_update_tool).pack(side='left',padx=(8,0))
        self.source_label=ttk.Label(header,text='');self.source_label.pack(side='left',padx=8)
        ttk.Label(root,text='「手動解析」でVfと近似領域を選択。「グラフ1〜6」で各計算過程を表示します（グラフ3は負電圧も表示）。',padding=(10,3)).grid(row=1,column=0,sticky='w')
        self.tabs=ttk.Notebook(root);self.tabs.grid(row=2,column=0,sticky='nsew')
        body=ttk.Frame(self.tabs,padding=(10,5));self.tabs.add(body,text='手動解析')
        self.stage_page=ttk.Frame(self.tabs);self.tabs.add(self.stage_page,text='グラフ1〜6')
        stage_controls=ttk.Frame(self.stage_page,padding=(8,5));stage_controls.pack(fill='x')
        self.stage_save_buttons=[]
        for text,all_stages in [('表示中のグラフをPNG保存',False),('グラフ1〜6をすべてPNG保存',True)]:
            button=ttk.Button(stage_controls,text=text,state='disabled',
                              command=lambda all_stages=all_stages:self.save_stage_graphs(all_stages))
            button.pack(side='left',padx=(0,8));self.stage_save_buttons.append(button)
        ttk.Label(stage_controls,text='保存先：データ別の outputs フォルダ内 / graphs').pack(side='left')
        self.stage_tabs=ttk.Notebook(self.stage_page);self.stage_tabs.pack(fill='both',expand=True)
        self.stage_frames=[];self.stage_views={}
        for label in STAGE_LABELS:
            frame=ttk.Frame(self.stage_tabs);frame.rowconfigure(0,weight=1);frame.columnconfigure(0,weight=1)
            self.stage_tabs.add(frame,text=label);self.stage_frames.append(frame)
        self.tabs.bind('<<NotebookTabChanged>>',lambda event:self.update_stage_graph())
        self.stage_tabs.bind('<<NotebookTabChanged>>',lambda event:self.update_stage_graph())
        body.columnconfigure(1,weight=1);body.rowconfigure(0,weight=1)
        left=ttk.Frame(body,width=330);left.grid(row=0,column=0,sticky='nsw',padx=(0,10))
        settings=ttk.LabelFrame(left,text='手動で指定する値',padding=8);settings.pack(fill='x')
        bounds=ttk.LabelFrame(left,text='適用範囲：Vin＋Vout [V]（境界は含めない）',padding=6)
        bounds.pack(fill='x',pady=5)
        ttk.Label(bounds,text='下限').grid(row=0,column=0)
        self.entry(bounds,'applicability_min_V',0,1)
        ttk.Label(bounds,text='上限').grid(row=0,column=2)
        self.entry(bounds,'applicability_max_V',0,3)
        ttk.Button(bounds,text='適用範囲を更新',command=self.refresh).grid(row=1,column=0,columnspan=4,sticky='ew')
        ttk.Label(settings,text='Vfの指定方法').grid(row=0,column=0,sticky='w')
        vf_options=ttk.Frame(settings);vf_options.grid(row=0,column=1,columnspan=2,sticky='w')
        self.vf_radio_buttons={}
        for text,value in [('正電圧の最小実測点（明瞭）','minimum'),('正電圧の小さい2点の平均','lowest_two')]:
            button=ttk.Radiobutton(vf_options,text=text,variable=self.vars['vf_mode'],value=value,command=self.vf_mode_changed)
            button.pack(anchor='w');self.vf_radio_buttons[value]=button
        self.row_pair(settings,'電子減速領域','retarding',1)
        self.row_pair(settings,'電子飽和領域','saturation',2)
        ttk.Label(settings,text='Vp前後の点・近似式・Iesは自動算出').grid(row=3,column=0,columnspan=3,sticky='w',pady=5)
        tools=ttk.LabelFrame(left,text='グラフの選択モード',padding=8);tools.pack(fill='x',pady=8)
        for i,(label,value) in enumerate([('電子減速領域をドラッグ','retarding'),('電子飽和領域をドラッグ','saturation')]):
            ttk.Radiobutton(tools,text=label,variable=self.mode,value=value,command=self.mode_changed).grid(row=i,column=0,sticky='w',pady=2)
        buttons=ttk.Frame(left);buttons.pack(fill='x',pady=4)
        ttk.Button(buttons,text='指定値でプレビュー更新',command=self.refresh).pack(fill='x')
        self.save_button=ttk.Button(buttons,text='設定・全結果CSV・全グラフを保存',command=self.save_and_export)
        self.save_button.pack(fill='x',pady=5)
        result_frame=ttk.LabelFrame(left,text='選択範囲の近似式・計算結果',padding=6);result_frame.pack(fill='both',expand=True,pady=4)
        self.result_text=tk.Text(result_frame,width=39,height=8,wrap='word',font=('Meiryo',9),state='disabled')
        scrollbar=ttk.Scrollbar(result_frame,command=self.result_text.yview)
        scrollbar.pack(side='right',fill='y');self.result_text.configure(yscrollcommand=scrollbar.set)
        self.result_text.pack(fill='both',expand=True)
        graph=ttk.Frame(body);graph.grid(row=0,column=1,sticky='nsew');graph.rowconfigure(0,weight=1);graph.columnconfigure(0,weight=1)
        self.figure=Figure(figsize=(9,6),layout='constrained');self.ax=self.figure.subplots()
        self.canvas=FigureCanvasTkAgg(self.figure,master=graph);self.canvas.get_tk_widget().grid(row=0,column=0,sticky='nsew')
        toolbar_frame=ttk.Frame(graph);toolbar_frame.grid(row=1,column=0,sticky='ew')
        self.toolbar=NavigationToolbar2Tk(self.canvas,toolbar_frame)
        self.span=SpanSelector(self.ax,self.on_span,'horizontal',useblit=True,props={'facecolor':'#657a91','alpha':.2},button=1)
        self.status=tk.StringVar(value='')
        self.status_label=ttk.Label(root,textvariable=self.status,padding=(10,8),wraplength=1250)
        self.status_label.grid(row=3,column=0,sticky='ew')
        self.refresh_catalog(choose_initial=False)
        if not self.initial_config.exists() or not self.load_profile(self.initial_config):
            if self.profiles:self.load_profile(next(iter(self.profiles.values())))
            else:self.clear_data()
        self._watch_id=root.after(5000,self.watch_data)

    def entry(self,parent,key,row,column,width=8,columnspan=1):
        widget=ttk.Entry(parent,textvariable=self.vars[key],width=width)
        widget.grid(row=row,column=column,columnspan=columnspan,sticky='w',padx=3,pady=3)
        widget.bind('<Return>',lambda event:self.refresh())
        return widget

    def open_update_tool(self):
        """解析画面を閉じ、依存ライブラリのない独立した更新ツールへ移る。"""
        if not messagebox.askyesno('アップデート',
                '解析画面を閉じて更新ツールを開きます。\n未保存の指定がある場合は、先に保存してください。\n\n更新ツールを開きますか？',parent=self.root):return
        try:
            options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
            subprocess.Popen([sys.executable,str(ROOT/'update_app.py'),'--gui','--target',str(ROOT)],
                             cwd=ROOT,**options)
        except OSError as error:
            messagebox.showerror('更新ツールを開けませんでした',str(error),parent=self.root)
            return
        self.close()

    def close(self):
        self._closed=True
        if self._watch_id is not None:
            self.root.after_cancel(self._watch_id)
            self._watch_id=None
        self._watch_pool.shutdown(wait=False,cancel_futures=True)
        self._selector_cache.clear()
        clear_measurement_cache()
        if hasattr(self,'span'):self.span.disconnect_events()
        self.root.update_idletasks()
        self.root.destroy()

    def row_pair(self,parent,label,prefix,row):
        ttk.Label(parent,text=label).grid(row=row,column=0,sticky='w',pady=3)
        return self.entry(parent,prefix+'_start',row,1),self.entry(parent,prefix+'_end',row,2)

    def refresh_catalog(self,force=True,choose_initial=True,*,snapshot=None):
        snapshot=data_snapshot(self.data_dir) if snapshot is None else snapshot
        if not force and snapshot==self._snapshot:return
        known=[*DEFAULT_CONFIGS,self.initial_config]
        if hasattr(self,'config_path'):known.append(self.config_path)
        profiles,errors=discover_profiles(self.data_dir,self.profile_dir,known)
        self._snapshot=snapshot;self.profiles=profiles;self.catalog_errors=errors
        # 明示的な一覧更新・ファイル追加時だけ全件の保存状態を再確認する。
        self._selector_cache.clear()
        current=getattr(self,'config_path',None)
        self.update_selector(current)
        label=next((name for name,path in profiles.items() if path==current),None)
        if label:
            if not self.has_trace and choose_initial:
                self.load_profile(current)
            elif self.has_trace:
                try:
                    info=self.source.stat();signature=(info.st_size,info.st_mtime_ns)
                    if signature!=self._trace_signature:
                        self.rows,self.x,self.trace=read_trace(self.source,self.base,details=True)
                        self.log=self.trace[2]
                        self._trace_signature=signature;self.refresh()
                except (OSError,ValueError,KeyError,TypeError) as error:
                    self.clear_data(f'選択中のCSVを読み込めません: {error}')
        elif choose_initial and profiles:
            self.load_profile(next(iter(profiles.values())))
        elif choose_initial and not profiles:
            self.clear_data()
        if errors:
            self.status.set(f'一覧更新：読み込めないファイル・設定が{len(errors)}件あります。{errors[0]}')

    def update_selector(self,current=None):
        """選択欄はフォルダ・CSV名を残し、実際に結果を保存済みかを付記する。"""
        self.selection_profiles={}
        for label,path in self.profiles.items():
            key=(label,path)
            if key not in self._selector_cache:
                saved=False
                try:
                    config=json.loads(path.read_text(encoding='utf-8-sig'))
                    source=(path.parent/config['input']).resolve()
                    saved=saved_output_directory(source,config,path,root=self.data_dir.parent) is not None
                except (OSError,ValueError,KeyError,TypeError):
                    pass
                self._selector_cache[key]=saved
            saved=self._selector_cache[key]
            relative=Path(label)
            name=relative.name
            if relative.parent!=Path('.'):
                name+=f'（{relative.parent.as_posix()}）'
            display=f'【{"保存済み" if saved else "未保存"}】 {name}'
            self.selection_profiles[display]=path
            if path==current:self.profile.set(display)
        self.selector.configure(values=list(self.selection_profiles))

    def watch_data(self):
        """走査だけを別スレッドで実行。Tkの操作は必ずGUIスレッドで行う。"""
        self._watch_id=None
        if self._closed:return
        if self._snapshot_future is None:
            self._snapshot_future=self._watch_pool.submit(data_snapshot,self.data_dir)
        if not self._snapshot_future.done():
            self._watch_id=self.root.after(100,self.watch_data)
            return
        try:
            snapshot=self._snapshot_future.result()
            self.refresh_catalog(force=False,snapshot=snapshot)
        except OSError as error:
            self.status.set(f'データ一覧の監視に失敗しました: {error}')
        finally:
            self._snapshot_future=None
            if not self._closed:self._watch_id=self.root.after(5000,self.watch_data)

    def clear_data(self,message='dataフォルダにLP測定の.csvを入れてください。自動で一覧へ追加します。'):
        self.has_trace=False;self.result=None;self.profile.set('')
        self.geometry_fits={};self.geometry_vp=None;self.geometry_error=''
        self.rows=np.array([],dtype=int);self.x=np.array([]);self.log=np.array([])
        self.source_label.configure(text='データ未選択')
        self.set_result_text('解析するデータを選択してください。')
        self.save_button.configure(state='disabled');self.status.set(message)
        self.ax.clear();self.ax.set(title='Add an LP512 .csv file to data/')
        self.span.set_active(False);self.canvas.draw_idle()
        self.update_stage_graph()

    def load_profile(self,path):
        self.loading=True
        try:
            config_path=Path(path).resolve()
            base=normalize_config(json.loads(config_path.read_text(encoding='utf-8-sig')))
            if base.get('floating_potential_mode') not in ('minimum','lowest_two'):base['floating_potential_mode']='unselected'
            source=(config_path.parent/base['input']).resolve()
            rows,x,trace=read_trace(source,base,details=True)
            log=trace[2]
            info=source.stat();signature=(info.st_size,info.st_mtime_ns)
        except (OSError,ValueError,KeyError,TypeError) as error:
            self.loading=False
            if self.has_trace:
                self.update_selector(self.config_path)
                self.status.set(f'データを読み込めません: {error}')
            else:self.clear_data(f'データを読み込めません: {error}')
            return False
        self.config_path=config_path;self.base=base;self.source=source
        # 古いハッシュ付きの保存先を使わず、dataと同じ階層・CSV名へ保存する。
        out=output_directory(source,base,config_path,root=self.data_dir.parent)
        self.base['output_dir']=Path(os.path.relpath(out,config_path.parent)).as_posix()
        self.rows,self.x,self.log=rows,x,log
        self.trace=trace
        self._trace_signature=signature;self.has_trace=True
        self.source_label.configure(text=f"{self.source.name} / CSVデータ {self.rows[0]}〜{self.rows[-1]}行")
        label=next((name for name,p in self.profiles.items() if p==config_path),source.name)
        self.profiles[label]=config_path;self.update_selector(config_path)
        for key in REGION_KEYS:
            pair=self.base.get(key+'_fit_rows') or ('','')
            for suffix,value in zip(('start','end'),pair):self.vars[key+'_'+suffix].set(str(value))
        self.vars['vf_mode'].set(self.base.get('floating_potential_mode','unselected'))
        for key in ('applicability_min_V','applicability_max_V'):
            self.vars[key].set(f'{self.base[key]:g}')
        self.loading=False;self.refresh()
        return True

    def get_config(self):
        entries={key:var.get().strip() for key,var in self.vars.items()}
        return selected_config(self.base,entries)

    def set_result_text(self,text):
        self.result_text.configure(state='normal');self.result_text.delete('1.0','end')
        self.result_text.insert('1.0',text);self.result_text.configure(state='disabled')

    def update_geometry(self):
        """Vf・Iesの指定や物理量の検証を待たず、ドラッグした直線の交点を更新。"""
        self.geometry_fits={};self.geometry_vp=None;self.geometry_error=''
        for key in ('retarding','saturation'):
            try:
                lo,hi=int(self.vars[key+'_start'].get()),int(self.vars[key+'_end'].get())
                if not self.rows[0]<=lo<hi<=self.rows[-1]:continue
                mask=(self.rows>=lo)&(self.rows<=hi)&(self.x>0)&np.isfinite(self.log)
                self.geometry_fits[key]=fit_line(self.x[mask],self.log[mask],(lo,hi))
            except ValueError:continue
        if not all(key in self.geometry_fits for key in ('retarding','saturation')):return
        try:
            self.geometry_vp=intersect_fits(self.geometry_fits['retarding'],self.geometry_fits['saturation'])
            self.geometry_fits['vp']=fit_vp_neighbors(self.x,self.log,self.rows,self.geometry_vp)
        except ValueError as error:self.geometry_error=str(error)

    def geometry_text(self):
        text=f'Vp = {self.geometry_vp:.6f} V（減速・飽和直線の交点）\n\n' if self.geometry_vp is not None else ''
        for key,label in [('retarding','電子減速'),('saturation','電子飽和'),('vp','Vp前後の近似')]:
            if key not in self.geometry_fits:continue
            fit=self.geometry_fits[key]
            text+=f'{label} {fit.rows[0]}〜{fit.rows[1]}行 ({fit.points}点)\nln|Ie| = {fit.slope:.6g}V {fit.intercept:+.6g}\n\n'
        if self.geometry_error:text+=self.geometry_error+'\n'
        return text

    def refresh(self):
        if self.loading:return
        if not self.has_trace:
            self.save_button.configure(state='disabled');return
        self.result=None
        try:
            bounds=applicability_bounds({key:self.vars[key].get().strip()
                                         for key in ('applicability_min_V','applicability_max_V')})
            if bounds != applicability_bounds(self.base):
                config=self.base | dict(zip(('applicability_min_V','applicability_max_V'),bounds))
                pair=select_applicable_rows(self.source,config)
                config['analysis_rows']=pair
                rows,x,trace=read_trace(self.source,config,details=True)
                if pair != self.base['analysis_rows']:
                    # 適用点が変わるとイオン直線・Vf・Vp0も変わるため、領域を選び直す。
                    for key in REGION_KEYS:
                        config[key+'_fit_rows']=None
                        for suffix in ('start','end'):self.vars[key+'_'+suffix].set('')
                    config['floating_potential_mode']='unselected'
                    self.vars['vf_mode'].set('unselected')
                self.base=config
                self.rows,self.x,self.trace,self.log=rows,x,trace,trace.log_current
                self.source_label.configure(text=f'{self.source.name} / CSVデータ {rows[0]}〜{rows[-1]}行')
        except (ValueError,KeyError,TypeError) as error:
            self.geometry_fits={};self.geometry_vp=None
            self.set_result_text(f'適用範囲を更新できません: {error}')
            self.save_button.configure(state='disabled');self.status.set(str(error));return
        self.update_geometry()
        self.set_result_text(self.geometry_text())
        self.save_button.configure(state='disabled')
        try:
            config=self.get_config()
            self.result=analyze(self.source,config)
            self.preview_config=config
            m=self.result.metrics
            text=(f"Vf = {m['floating_potential_V']:.6f} V\nVp0 = {m['derivative_peak_voltage_V']:.6f} V\nVp = {m['plasma_potential_V']:.6f} V\n"
                  f"Te = {m['electron_temperature_K']:,.3f} K\nTe = {m['electron_temperature_eV']:.6f} eV\n"
                  f"ne = {m['electron_density_cm3']:,.3f} /cm³\n\n")
            for key,label in [('ion','イオン'),('retarding','電子減速'),('saturation','電子飽和'),('vp','Vp前後の自動近似')]:
                f=self.result.fits[key]
                r2='n/a' if f.r_squared is None else f'{f.r_squared:.6f}'
                text+=f'{label} {f.rows[0]}〜{f.rows[1]}行 ({f.points}点)\n'+('Ipol[A]' if key=='ion' else 'ln|Ie|')+f' = {f.slope:.6g}V {f.intercept:+.6g}\nR² = {r2}\n\n'
            self.set_result_text(text)
            self.status.set('プレビュー更新。保存ボタンを押すと、現在の指定値とCSV・グラフを書き出します。')
            self.save_button.configure(state='normal')
        except (ValueError,KeyError,TypeError,OverflowError) as error:
            text=self.geometry_text()+'Vfなどの指定が完了すると、電子温度・電子密度を表示します。'
            text+=f'\nVp0 = {self.x[self.trace.peak_index]:.6f} V\n'
            fit=self.trace.ion_fit
            text+=f'イオン近似 ({fit.points}点):\nIpol[A] = {fit.slope:.6g}V {fit.intercept:+.6g}\n'
            self.set_result_text(text)
            self.status.set(str(error));self.save_button.configure(state='disabled')
        self.draw()
        self.update_stage_graph()

    def update_stage_graph(self):
        """表示中のグラフだけ遅延描画。隠れている6枚をドラッグごとに作り直さない。"""
        if self._closed or self.tabs.select()!=str(self.stage_page):return False
        for button in self.stage_save_buttons:
            button.configure(state='normal' if self.has_trace else 'disabled')
        index=self.stage_tabs.index('current')
        if index not in self.stage_views:
            frame=self.stage_frames[index]
            figure=Figure(figsize=(16,9) if index==6 else (10,6),layout='constrained')
            axes=list(figure.subplots(2,3).flat) if index==6 else [figure.subplots()]
            canvas=FigureCanvasTkAgg(figure,master=frame)
            canvas.get_tk_widget().grid(row=0,column=0,sticky='nsew')
            toolbar_frame=ttk.Frame(frame);toolbar_frame.grid(row=1,column=0,sticky='ew')
            toolbar=NavigationToolbar2Tk(canvas,toolbar_frame)
            self.stage_views[index]={'figure':figure,'ax':axes[0],'axes':axes,
                                     'canvas':canvas,'toolbar':toolbar,'key':None}
        view=self.stage_views[index]
        key=(str(self.source),self._trace_signature,tuple(self.base['analysis_rows']),
             applicability_bounds(self.base),self.base.get('resistance_ohm'),
             self.vars['vf_mode'].get() if index in (4,6) else None) if self.has_trace else ('empty',)
        if view['key']==key:return self.has_trace
        for ax in view['axes']:ax.clear()
        if not self.has_trace:
            view['ax'].set(title='Select an LP512 CSV file')
        else:
            try:
                for stage,ax in (enumerate(view['axes']) if index==6 else [(index,view['ax'])]):
                    self.draw_stage(ax,stage)
            except (OSError,ValueError,KeyError,TypeError) as error:
                self.status.set(f'{STAGE_LABELS[index]}を表示できません: {error}')
                view['canvas'].draw_idle()
                return False
        view['key']=key
        view['canvas'].draw_idle()
        return self.has_trace

    def draw_stage(self,ax,index):
        """表示・個別保存・6枚保存に同じデータと描画処理を使う。"""
        data=read_points(self.source,self.base,include_all=True,metadata=True)
        selected=np.isin(data[0],self.rows)
        vf=None
        mode=self.vars['vf_mode'].get()
        if index==4 and mode in ('minimum','lowest_two'):
            try:
                vf=minimum_floating_potential(self.x,self.log,self.rows,
                                              average_two=mode=='lowest_two',positive_only=True).voltage_V
            except ValueError:
                pass  # Vfをまだ決められなくても、lnの測定点は表示する。
        draw_preprocessing_stage(ax,index,self.x,data[2][selected],self.trace,
                                 data[:3],data[3],applicability_bounds(self.base),vf)

    def save_stage_graphs(self,all_stages=False):
        """1枚は現在のズームで保存。6枚出力は画面を切り替えず標準表示で個別PNGにする。"""
        if not self.has_trace:
            self.status.set('解析するデータを選択してください。')
            return False
        out=output_directory(self.source,self.base,self.config_path,root=self.data_dir.parent)/'graphs'
        self.status.set('グラフをPNGに保存しています…');self.root.update_idletasks()
        try:
            out.mkdir(parents=True,exist_ok=True)
            if all_stages:
                for index,(name,_) in enumerate(PREPROCESSING_STAGES):
                    figure=Figure(figsize=(10,6),layout='constrained');FigureCanvasAgg(figure)
                    try:
                        self.draw_stage(figure.subplots(),index)
                        figure.savefig(out/f'{name}.png',dpi=160)
                    finally:
                        figure.clear()
                detail='グラフ1〜6を6枚のPNGで保存'
            else:
                index=self.stage_tabs.index('current')
                if not self.update_stage_graph():return False
                view=self.stage_views[index];view['canvas'].draw()
                name='01-06_overview.png' if index==6 else PREPROCESSING_STAGES[index][0]+'.png'
                view['figure'].savefig(out/name,dpi=160)
                detail=f'表示中のグラフを保存: {name}'
            self.status.set(f'{detail} ／ 保存先: {out}')
            return True
        except (OSError,ValueError,KeyError,TypeError) as error:
            self.status.set(f'グラフの保存に失敗: {error}')
            messagebox.showerror('グラフを保存できませんでした',str(error),parent=self.root)
            return False

    def draw(self):
        self.ax.clear()
        visible=(self.x>0)&np.isfinite(self.log)
        self.ax.plot(self.x[visible],self.log[visible],'o',linestyle='None',ms=5,color='#526170',label='ln(|Ie| / 1 A), Vout > 0')
        if 'vp' in self.geometry_fits:
            fit=self.geometry_fits['vp'];mask=(self.rows>=fit.rows[0])&(self.rows<=fit.rows[1])&visible
            self.ax.scatter(self.x[mask],self.log[mask],s=55,color=COLORS['vp'],label='Vp neighbor points',zorder=5)
            self.ax.plot(self.x[mask],fit.at(self.x[mask]),'-',lw=2.3,color=COLORS['vp'],label=f'Vp neighbors fit: y={fit.slope:.4g}x {fit.intercept:+.4g}')
        for key,label in [('retarding','Retarding'),('saturation','Saturation')]:
            try:
                lo=int(self.vars[key+'_start'].get());hi=int(self.vars[key+'_end'].get())
            except ValueError:continue
            mask=(self.rows>=lo)&(self.rows<=hi)&visible
            if mask.any():
                self.ax.scatter(self.x[mask],self.log[mask],color=COLORS[key],s=40,label=f'{label} [{lo}:{hi}]',zorder=4)
                self.ax.axvspan(self.x[mask].min(),self.x[mask].max(),color=COLORS[key],alpha=.08)
            if mask.sum()>=2:
                try:
                    fit=self.geometry_fits[key] if key in ('retarding','saturation') and key in self.geometry_fits else fit_line(self.x[mask],self.log[mask],(lo,hi))
                except ValueError:
                    continue
                vp=self.geometry_vp if self.geometry_vp is not None else float(self.x[mask].mean())
                left,right=min(self.x[mask].min(),vp),max(self.x[mask].max(),vp)
                positive=self.x[self.x>0]
                margin=max((right-left)*.08,float(np.ptp(positive))*.08)
                xx=np.linspace(max(float(positive.min()),left-margin),max(float(positive.min()),right+margin),70)
                self.ax.plot(xx,fit.at(xx),'-',color=COLORS[key],lw=2.3,
                             label=f'{label} fit: y={fit.slope:.4g}x {fit.intercept:+.4g}')
        if self.geometry_vp is not None and self.geometry_vp>0:
            vp=self.geometry_vp;intersection=self.geometry_fits['retarding'].at(vp)
            self.ax.axvline(vp,color='black',ls='--',label=f'Vp = {vp:.6f} V')
            self.ax.scatter(vp,intersection,marker='*',s=110,color='black',label='Vp intersection',zorder=7)
        if self.result:
            self.ax.axvline(self.result.floating_potential.voltage_V,color='#8a348a',ls=':',label=f'Vf = {self.result.floating_potential.voltage_V:.6f} V')
            self.ax.scatter(self.result.metrics['plasma_potential_V'],self.result.metrics['log_electron_saturation_current'],marker='D',s=40,color=COLORS['vp'],label='Ies at Vp',zorder=5)
        else:
            try:
                if self.vars['vf_mode'].get() in ('minimum','lowest_two'):
                    floating=minimum_floating_potential(self.x,self.log,self.rows,average_two=self.vars['vf_mode'].get()=='lowest_two',positive_only=True)
                    v=floating.voltage_V
                    indices=[list(self.rows).index(row) for row in floating.rows]
                    self.ax.scatter(self.x[indices],self.log[indices],color='#8a348a',s=65,zorder=6,label='Vf selected points')
                    if math.isfinite(v):self.ax.axvline(v,color='#8a348a',ls=':',label=f'Vf = {v:.6f} V')
            except (ValueError,IndexError):pass
        if self.x[self.trace.peak_index]>0:
            self.ax.axvline(self.x[self.trace[5]],color='#b4473b',ls='-.',label=f'Vp0 = {self.x[self.trace[5]]:.6f} V')
        self.ax.set(title='Manual selection: ln|Ie| vs Vout',xlabel='Vout (V)',ylabel='ln(|Ie| / 1 A)')
        self.ax.set_xlim(left=0)
        self.ax.grid(alpha=.2);self.ax.legend(fontsize=8,loc='best')
        self.span.disconnect_events()
        self.span=SpanSelector(self.ax,self.on_span,'horizontal',useblit=True,props={'facecolor':'#657a91','alpha':.2},button=1)
        self.mode_changed();self.canvas.draw_idle()

    def mode_changed(self):
        self.span.set_active(self.mode.get() in REGION_KEYS)

    def vf_mode_changed(self):
        self.refresh()

    def on_span(self,left,right):
        if not self.has_trace:return
        if self.mode.get() not in REGION_KEYS or self.toolbar.mode:return
        selected=self.rows[(self.x>=min(left,right))&(self.x<=max(left,right))&(self.x>0)&np.isfinite(self.log)]
        if len(selected)<2:
            self.status.set('近似する点が2点以上入るようにドラッグしてください。');return
        key=self.mode.get()
        self.vars[key+'_start'].set(str(int(selected.min())))
        self.vars[key+'_end'].set(str(int(selected.max())))
        self.refresh()

    def save_and_export(self):
        # 指定値が変わっていなければ再描画せず、表示中のズーム・範囲をそのまま保存する。
        try:
            changed=self.get_config()!=getattr(self,'preview_config',None)
        except (ValueError,KeyError,TypeError):
            changed=True
        if self.result is None or changed:self.refresh()
        if self.result is None:return False
        config=copy.deepcopy(self.preview_config)
        out=output_directory(self.source,config,self.config_path,root=self.data_dir.parent)
        self.status.set('現在の手動指定で解析結果を書き出しています…');self.root.update_idletasks()
        try:
            export_analysis(self.source,config,out,self.result)
            self.canvas.draw()
            self.figure.savefig(out/'graphs'/'11_gui_view.png',dpi=160)
            save_config(self.config_path,config)
            self.base=config
            self._selector_cache={key:value for key,value in self._selector_cache.items()
                                  if key[1]!=self.config_path}
            self.update_selector(self.config_path)
            self.status.set(f'保存完了: {out} ／ 日本語の結果一覧: results_summary.csv ／ GUIグラフ: graphs/11_gui_view.png')
            return True
        except (OSError,ValueError) as error:
            self.status.set(f'書き出しに失敗: {error}')
            messagebox.showerror('解析結果を書き出せませんでした',str(error),parent=self.root)
            return False


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=ROOT/'analysis_config.json')
    args=parser.parse_args(argv)
    root=tk.Tk()
    ManualAnalysisApp(root,args.config)
    root.mainloop()
    return 0


if __name__=='__main__':
    raise SystemExit(main())

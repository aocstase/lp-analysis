"""ln|Ie|–Voutを見ながらVf・近似領域を利用者が選択する画面。"""
from __future__ import annotations

import argparse
import copy
import json
import math
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

import numpy as np
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.widgets import SpanSelector

from analyze import analyze, export_analysis
from lp_calculation import (ROOT, read_trace, fit_line, intersect_fits, fit_vp_neighbors,
                            normalize_config, minimum_floating_potential)
from data_catalog import DEFAULT_CONFIGS, data_snapshot, discover_profiles
from lp_calculation import output_directory, applicability_bounds, select_applicable_rows

COLORS = {'retarding':'#246caf','saturation':'#ce721b','vp':'#398147'}
REGION_KEYS=('retarding','saturation')


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
        self.profiles={};self.catalog_errors=[];self._snapshot=None
        self._watch_id=None;self._closed=False;self.has_trace=False
        root.title('PRELUDE_MIS — ln表示から手動解析')
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
        self.selector=ttk.Combobox(header,textvariable=self.profile,values=[],state='readonly',width=45,
                                 postcommand=self.refresh_catalog)
        self.selector.pack(side='left',padx=8)
        self.selector.bind('<<ComboboxSelected>>',lambda event:self.load_profile(self.profiles[self.profile.get()]))
        ttk.Button(header,text='一覧更新',command=self.refresh_catalog).pack(side='left')
        self.source_label=ttk.Label(header,text='');self.source_label.pack(side='left',padx=8)
        ttk.Label(root,text='Vout > 0 Vを表示。Vfの方法を選び、近似領域をドラッグしてください。イオン近似は負電圧の測定点から毎回計算します。',padding=(10,3)).grid(row=1,column=0,sticky='w')
        body=ttk.Frame(root,padding=(10,5));body.grid(row=2,column=0,sticky='nsew')
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
        self._watch_id=root.after(1500,self.watch_data)

    def entry(self,parent,key,row,column,width=8,columnspan=1):
        widget=ttk.Entry(parent,textvariable=self.vars[key],width=width)
        widget.grid(row=row,column=column,columnspan=columnspan,sticky='w',padx=3,pady=3)
        widget.bind('<Return>',lambda event:self.refresh())
        return widget

    def close(self):
        self._closed=True
        if self._watch_id is not None:
            self.root.after_cancel(self._watch_id)
            self._watch_id=None
        if hasattr(self,'span'):self.span.disconnect_events()
        self.root.update_idletasks()
        self.root.destroy()

    def row_pair(self,parent,label,prefix,row):
        ttk.Label(parent,text=label).grid(row=row,column=0,sticky='w',pady=3)
        return self.entry(parent,prefix+'_start',row,1),self.entry(parent,prefix+'_end',row,2)

    def refresh_catalog(self,force=True,choose_initial=True):
        snapshot=data_snapshot(self.data_dir)
        if not force and snapshot==self._snapshot:return
        known=[*DEFAULT_CONFIGS,self.initial_config]
        if hasattr(self,'config_path'):known.append(self.config_path)
        profiles,errors=discover_profiles(self.data_dir,self.profile_dir,known)
        self._snapshot=snapshot;self.profiles=profiles;self.catalog_errors=errors
        self.selector.configure(values=list(profiles))
        current=getattr(self,'config_path',None)
        label=next((name for name,path in profiles.items() if path==current),None)
        if label:
            self.profile.set(label)
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

    def watch_data(self):
        self._watch_id=None
        if self._closed:return
        try:
            self.refresh_catalog(force=False)
        finally:
            if not self._closed:self._watch_id=self.root.after(1500,self.watch_data)

    def clear_data(self,message='dataフォルダにLP測定の.csvを入れてください。自動で一覧へ追加します。'):
        self.has_trace=False;self.result=None;self.profile.set('')
        self.geometry_fits={};self.geometry_vp=None;self.geometry_error=''
        self.rows=np.array([],dtype=int);self.x=np.array([]);self.log=np.array([])
        self.source_label.configure(text='データ未選択')
        self.set_result_text('解析するデータを選択してください。')
        self.save_button.configure(state='disabled');self.status.set(message)
        self.ax.clear();self.ax.set(title='Add an LP512 .csv file to data/')
        self.span.set_active(False);self.canvas.draw_idle()

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
                self.profile.set(next((name for name,p in self.profiles.items() if p==self.config_path),self.source.name))
                self.status.set(f'データを読み込めません: {error}')
            else:self.clear_data(f'データを読み込めません: {error}')
            return False
        self.config_path=config_path;self.base=base;self.source=source
        self.rows,self.x,self.log=rows,x,log
        self.trace=trace
        self._trace_signature=signature;self.has_trace=True
        self.source_label.configure(text=f"{self.source.name} / CSVデータ {self.rows[0]}〜{self.rows[-1]}行")
        label=next((name for name,p in self.profiles.items() if p==config_path),source.name)
        self.profiles[label]=config_path;self.profile.set(label)
        self.selector.configure(values=list(self.profiles))
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
        self.draw();self.root.update_idletasks()
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
        out=output_directory(self.source,config,self.config_path)
        self.status.set('現在の手動指定で解析結果を書き出しています…');self.root.update_idletasks()
        try:
            export_analysis(self.source,config,out,self.result)
            self.canvas.draw()
            self.figure.savefig(out/'graphs'/'11_gui_view.png',dpi=160)
            save_config(self.config_path,config)
            self.base=config
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

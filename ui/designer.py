"""
OSA Sound Designer — interactive GUI for designing, previewing and exporting
intervention sound stimuli.
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import dearpygui.dearpygui as dpg
import sounddevice as sd
import soundfile as sf

from sounds.strategies import (
    STRATEGY_REGISTRY, synthesize, get_default_params,
)
from sounds.generator import DEFAULT_SR

PRESETS_DIR = Path(__file__).resolve().parent.parent / 'presets'
OUTPUT_DIR = Path(__file__).resolve().parent.parent / 'output'
STRATEGY_ORDER = ['P1', 'P2', 'P3', 'L1', 'L2']

# ── Colors ──
C_BG       = (24, 24, 28)
C_PANEL    = (34, 34, 40)
C_BORDER   = (52, 52, 60)
C_TEXT     = (225, 225, 230)
C_DIM      = (130, 130, 145)
C_BLUE     = (82, 148, 255)
C_BLUE_H   = (105, 168, 255)
C_BLUE_A   = (62, 128, 235)
C_GREEN    = (72, 199, 115)
C_GREEN_H  = (92, 219, 135)
C_GREEN_A  = (52, 179, 95)
C_RED      = (235, 75, 75)
C_RED_H    = (255, 95, 95)
C_RED_A    = (215, 55, 55)
C_AMBER    = (245, 175, 45)
C_AMBER_H  = (255, 190, 65)
C_AMBER_A  = (225, 155, 25)
C_CARD     = (44, 44, 52)
C_CARD_H   = (54, 54, 64)
C_SLIDER   = (50, 50, 58)
C_GRAB     = C_BLUE

# ── Per-strategy UI labels ──
STRAT_BTN = {
    'P1': 'P1 Whisper Sweep',
    'P2': 'P2 Double Pulse',
    'P3': 'P3 Low-Freq Rumble',
    'L1': 'L1 Smooth Burst',
    'L2': 'L2 Rough Burst',
}


def _find_chinese_font() -> Optional[str]:
    for p in [
        '/System/Library/Fonts/Supplemental/Arial Unicode.ttf',
        '/System/Library/Fonts/STHeiti Medium.ttc',
        'C:/Windows/Fonts/msyh.ttc',
        'C:/Windows/Fonts/simhei.ttf',
        '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    ]:
        if os.path.exists(p):
            return p
    return None


class SoundDesigner:

    PLAY_REPEATS = 3
    PLAY_GAP = 0.5

    def __init__(self):
        self.sr = DEFAULT_SR
        self.cur_strat: str = 'P1'
        self.cur_dir: str = 'left'
        self.params: Dict[str, float] = get_default_params('P1')
        self.waveform: Optional[np.ndarray] = None
        self._noise_seed: int = 42
        self._playing = False
        self._play_t0 = 0.0
        self._play_dur = 0.0

        self._slider_tags: Dict[str, int] = {}
        self._param_groups: list = []
        self._presets: list = []

        PRESETS_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── synth ──

    def _new_seed(self):
        self._noise_seed = int(time.time() * 1000) % (2**31)

    def _synth(self):
        sdef = STRATEGY_REGISTRY[self.cur_strat]
        d = self.cur_dir if sdef.has_direction else 'center'
        self.waveform = synthesize(self.cur_strat, self.params, d, self.sr,
                                   seed=self._noise_seed)
        self._update_plots()
        ms = len(self.waveform) / self.sr * 1000
        self._status(f"已生成  {ms:.0f} ms  /  {self.sr} Hz")

    def _play_wav(self):
        if self.waveform is None:
            self._synth()
        sd.stop()
        gap = np.zeros((int(self.PLAY_GAP * self.sr), 2))
        pad = np.zeros((int(0.12 * self.sr), 2))
        parts = [pad]
        for i in range(self.PLAY_REPEATS):
            parts.append(self.waveform)
            if i < self.PLAY_REPEATS - 1:
                parts.append(gap)
        parts.append(pad)
        full = np.concatenate(parts)
        self._play_dur = len(full) / self.sr
        sd.play(full, self.sr)
        self._playing = True
        self._play_t0 = time.time()
        dpg.configure_item(self._btn_play, label="  播放中...  ")
        dpg.show_item(self._progress)
        ms = len(self.waveform) / self.sr * 1000
        self._status(f"播放中  x{self.PLAY_REPEATS}  |  单次 {ms:.0f} ms")

    def _stop_wav(self):
        sd.stop()
        self._playing = False
        dpg.configure_item(self._btn_play, label="  播放  ")
        dpg.set_value(self._progress, 0)
        dpg.hide_item(self._progress)
        self._status("已停止")

    def _tick(self):
        if not self._playing:
            return
        t = time.time() - self._play_t0
        p = min(t / self._play_dur, 1.0)
        dpg.set_value(self._progress, p)
        if p >= 1.0:
            self._playing = False
            dpg.configure_item(self._btn_play, label="  播放  ")
            self._status("播放完成")

    # ── export ──

    def _export(self):
        if self.waveform is None:
            self._synth()
        sdef = STRATEGY_REGISTRY[self.cur_strat]
        d = self.cur_dir if sdef.has_direction else 'center'
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        name = f"{self.cur_strat}_{d}_{ts}.wav"
        sf.write(str(OUTPUT_DIR / name), self.waveform, self.sr)
        dpg.set_value(self._txt_status, f">>> 已导出  output/{name}")
        dpg.configure_item(self._txt_status, color=C_AMBER)
        self._export_flash_t = time.time()

    def _check_export_flash(self):
        if hasattr(self, '_export_flash_t') and self._export_flash_t > 0:
            if time.time() - self._export_flash_t > 3.0:
                dpg.configure_item(self._txt_status, color=C_GREEN)
                self._export_flash_t = 0

    def _batch(self):
        levels = [-30, -20, -10, -6]
        n = 0
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        for sk in STRATEGY_ORDER:
            sd_ = STRATEGY_REGISTRY[sk]
            pr = get_default_params(sk)
            dirs = ['left', 'right'] if sd_.has_direction else ['center']
            for d in dirs:
                for lv in levels:
                    pr['level_db'] = lv
                    w = synthesize(sk, pr, d, self.sr,
                                   seed=self._noise_seed)
                    sf.write(str(OUTPUT_DIR / f"{sk}_{d}_{lv}dB_{ts}.wav"),
                             w, self.sr)
                    n += 1
        dpg.set_value(self._txt_status, f">>> 批量导出完成  共 {n} 个文件 -> output/")
        dpg.configure_item(self._txt_status, color=C_AMBER)
        self._export_flash_t = time.time()

    # ── presets ──

    def _save_preset(self):
        name = dpg.get_value(self._inp_name).strip()
        if not name:
            self._status("请输入预设名称")
            return
        note = dpg.get_value(self._inp_note).strip()
        sdef = STRATEGY_REGISTRY[self.cur_strat]
        obj = {
            'name': name,
            'strategy': self.cur_strat,
            'direction': self.cur_dir if sdef.has_direction else 'center',
            'created_at': datetime.now().isoformat(timespec='seconds'),
            'note': note,
            'params': dict(self.params),
            'seed': self._noise_seed,
        }
        safe = "".join(c if c.isalnum() or c in '-_' else '_' for c in name)
        with open(PRESETS_DIR / f"{safe}.json", 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        self._status(f"已保存预设: {name}")
        dpg.set_value(self._inp_name, '')
        dpg.set_value(self._inp_note, '')
        self._reload_presets()

    def _load_preset(self):
        sel = dpg.get_value(self._combo_preset)
        if not sel:
            return
        for p in self._presets:
            if self._pdisp(p) == sel:
                self.cur_strat = p['strategy']
                self.params = dict(p['params'])
                self._noise_seed = p.get('seed', 42)
                sdef = STRATEGY_REGISTRY[self.cur_strat]
                if sdef.has_direction:
                    self.cur_dir = p.get('direction', 'left')
                self._refresh_strat()
                self._refresh_params()
                self._synth()
                self._status(f"已加载: {p['name']}")
                return

    def _del_preset(self):
        sel = dpg.get_value(self._combo_preset)
        if not sel:
            return
        for p in self._presets:
            if self._pdisp(p) == sel:
                safe = "".join(
                    c if c.isalnum() or c in '-_' else '_' for c in p['name'])
                fp = PRESETS_DIR / f"{safe}.json"
                if fp.exists():
                    fp.unlink()
                self._status(f"已删除: {p['name']}")
                self._reload_presets()
                return

    def _reload_presets(self):
        self._presets = []
        for f in sorted(PRESETS_DIR.glob('*.json')):
            try:
                with open(f, 'r', encoding='utf-8') as fh:
                    self._presets.append(json.load(fh))
            except Exception:
                continue
        items = [self._pdisp(p) for p in self._presets]
        dpg.configure_item(self._combo_preset, items=items)
        dpg.set_value(self._combo_preset, items[0] if items else '')

    @staticmethod
    def _pdisp(p: dict) -> str:
        ts = p.get('created_at', '')[:16].replace('T', ' ')
        n = f" | {p['note']}" if p.get('note') else ''
        return f"[{p['strategy']}] {p['name']}  ({ts}){n}"

    # ── plots ──

    def _update_plots(self):
        if self.waveform is None:
            return
        n = len(self.waveform)
        step = max(1, n // 2000)
        t = np.arange(0, n, step) / self.sr * 1000
        L = self.waveform[::step, 0]
        R = self.waveform[::step, 1]
        m = min(len(t), len(L), len(R))
        t, L, R = t[:m], L[:m], R[:m]
        dpg.set_value(self._line_l, [t.tolist(), L.tolist()])
        dpg.set_value(self._line_r, [t.tolist(), R.tolist()])

        peak = max(np.max(np.abs(L)), np.max(np.abs(R)), 0.01)
        margin = peak * 1.15
        t_max = t[-1] if len(t) > 0 else 1.0

        dpg.set_axis_limits(self._x_ax, 0, t_max)
        dpg.set_axis_limits(self._y_ax, -margin, margin)

    # ── ui helpers ──

    def _status(self, msg: str):
        dpg.set_value(self._txt_status, msg)
        dpg.configure_item(self._txt_status, color=C_GREEN)
        if hasattr(self, '_export_flash_t'):
            self._export_flash_t = 0

    def _on_strat(self, _s, _v, key):
        if key == self.cur_strat:
            return
        self.cur_strat = key
        self.params = get_default_params(key)
        self._new_seed()
        sdef = STRATEGY_REGISTRY[key]
        if not sdef.has_direction:
            self.cur_dir = 'center'
        elif self.cur_dir == 'center':
            self.cur_dir = 'left'
        self._refresh_strat()
        self._refresh_params()
        self._synth()

    def _refresh_strat(self):
        for k, b in self._sbtns.items():
            dpg.bind_item_theme(b, self._th_sel if k == self.cur_strat
                                else self._th_card)
        sdef = STRATEGY_REGISTRY[self.cur_strat]
        dpg.set_value(self._txt_desc, sdef.description)
        if sdef.has_direction:
            dpg.show_item(self._grp_dir)
            self._refresh_dir()
        else:
            dpg.hide_item(self._grp_dir)

    def _on_dir(self, _s, _v, d):
        self.cur_dir = d
        self._refresh_dir()
        self._synth()

    def _refresh_dir(self):
        for d, b in self._dbtns.items():
            dpg.bind_item_theme(b, self._th_amber if d == self.cur_dir
                                else self._th_card)

    def _on_param(self, _s, val, key):
        self.params[key] = val
        self._synth()

    def _refresh_params(self):
        for t in self._param_groups:
            if dpg.does_item_exist(t):
                dpg.delete_item(t)
        self._param_groups.clear()
        self._slider_tags.clear()

        sdef = STRATEGY_REGISTRY[self.cur_strat]
        groups: Dict[str, list] = {}
        for ps in sdef.params:
            groups.setdefault(ps.group, []).append(ps)

        glabel = {'general': '基本参数', 'spatial': '空间化参数',
                  'roughness': '粗糙度调制'}

        for gk in ['general', 'spatial', 'roughness']:
            specs = groups.get(gk)
            if not specs:
                continue
            g = dpg.add_group(parent=self._params_box)
            self._param_groups.append(g)
            dpg.add_spacer(height=3, parent=g)
            dpg.add_text(glabel.get(gk, gk), color=C_BLUE, parent=g)

            for ps in specs:
                v = self.params.get(ps.key, ps.default)
                u = f" {ps.unit}" if ps.unit else ''
                fmt = f"%.0f{u}" if ps.step >= 1 else (
                    f"%.1f{u}" if ps.step >= 0.1 else f"%.2f{u}")
                tag = dpg.add_slider_float(
                    label=f"  {ps.label}",
                    default_value=v, min_value=ps.min_val,
                    max_value=ps.max_val, format=fmt,
                    callback=self._on_param, user_data=ps.key,
                    width=380, parent=g)
                self._slider_tags[ps.key] = tag

    # ── themes ──

    def _themes(self):
        with dpg.theme() as self._th_global:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg, C_BG)
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, C_PANEL)
                dpg.add_theme_color(dpg.mvThemeCol_Text, C_TEXT)
                dpg.add_theme_color(dpg.mvThemeCol_Border, C_BORDER)
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, C_SLIDER)
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, (60, 60, 68))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, (70, 70, 78))
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrab, C_GRAB)
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrabActive, C_BLUE_A)
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarBg, (28, 28, 32))
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 5)
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 10)
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 18, 14)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 8, 5)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 8, 5)

        def _btn(bg, bg_h, bg_a, fg=C_TEXT, pad=(14, 9), rnd=7):
            th = dpg.add_theme()
            with dpg.theme_component(dpg.mvButton, parent=th):
                dpg.add_theme_color(dpg.mvThemeCol_Button, bg)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, bg_h)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, bg_a)
                dpg.add_theme_color(dpg.mvThemeCol_Text, fg)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, rnd)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, *pad)
            return th

        self._th_card  = _btn(C_CARD, C_CARD_H, (64, 64, 74), C_DIM)
        self._th_sel   = _btn(C_BLUE, C_BLUE_H, C_BLUE_A, (255,255,255))
        self._th_amber = _btn(C_AMBER, C_AMBER_H, C_AMBER_A, (25,25,25))
        self._th_play  = _btn(C_GREEN, C_GREEN_H, C_GREEN_A, (255,255,255),
                              (30, 12), 8)
        self._th_stop  = _btn(C_RED, C_RED_H, C_RED_A, (255,255,255),
                              (30, 12), 8)
        self._th_sec   = _btn((48, 48, 56), (58, 58, 68), (68, 68, 78),
                              C_TEXT, (14, 9), 6)

        with dpg.theme() as self._th_prog:
            with dpg.theme_component(dpg.mvProgressBar):
                dpg.add_theme_color(dpg.mvThemeCol_PlotHistogram, C_GREEN)
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, C_SLIDER)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 4)

        with dpg.theme() as self._th_wl:
            with dpg.theme_component(dpg.mvLineSeries):
                dpg.add_theme_color(dpg.mvPlotCol_Line, C_BLUE)
        with dpg.theme() as self._th_wr:
            with dpg.theme_component(dpg.mvLineSeries):
                dpg.add_theme_color(dpg.mvPlotCol_Line, C_AMBER)

    # ── build ──

    def build(self):
        dpg.create_context()

        fp = _find_chinese_font()
        if fp:
            with dpg.font_registry():
                with dpg.font(fp, 17) as f_body:
                    dpg.add_font_range_hint(dpg.mvFontRangeHint_Chinese_Full)
                    dpg.add_font_range_hint(dpg.mvFontRangeHint_Default)
                with dpg.font(fp, 26) as f_title:
                    dpg.add_font_range_hint(dpg.mvFontRangeHint_Default)
                    dpg.add_font_range_hint(dpg.mvFontRangeHint_Chinese_Full)
            dpg.bind_font(f_body)
        else:
            f_title = None

        self._themes()

        with dpg.window(tag='main'):

            # ─── 标题 ───
            tt = dpg.add_text("OSA Sound Designer", color=C_BLUE)
            if f_title:
                dpg.bind_item_font(tt, f_title)
            dpg.add_spacer(height=6)
            dpg.add_separator()
            dpg.add_spacer(height=4)

            # ─── 策略选择 ───
            dpg.add_text("选择声音策略", color=C_DIM)
            dpg.add_spacer(height=3)
            row = dpg.add_group(horizontal=True)
            self._sbtns: Dict[str, int] = {}
            for k in STRATEGY_ORDER:
                b = dpg.add_button(
                    label=f" {STRAT_BTN[k]} ",
                    callback=self._on_strat, user_data=k, parent=row)
                self._sbtns[k] = b
            dpg.add_spacer(height=3)
            self._txt_desc = dpg.add_text(
                STRATEGY_REGISTRY['P1'].description, color=C_DIM)

            dpg.add_spacer(height=6)
            dpg.add_separator()
            dpg.add_spacer(height=4)

            # ─── 方向 ───
            self._grp_dir = dpg.add_group()
            dpg.add_text("播放方向", color=C_DIM, parent=self._grp_dir)
            dpg.add_spacer(height=3, parent=self._grp_dir)
            dr = dpg.add_group(horizontal=True, parent=self._grp_dir)
            self._dbtns: Dict[str, int] = {}
            for d, lb in [('left', '  左声道  '), ('right', '  右声道  ')]:
                b = dpg.add_button(label=lb, callback=self._on_dir,
                                   user_data=d, parent=dr)
                self._dbtns[d] = b
            dpg.add_spacer(height=6, parent=self._grp_dir)
            self._dir_sep = dpg.add_separator(parent=self._grp_dir)
            dpg.add_spacer(height=4, parent=self._grp_dir)

            # ─── 参数 ───
            dpg.add_text("参数调节", color=C_DIM)
            self._params_box = dpg.add_group()

            dpg.add_spacer(height=6)
            dpg.add_separator()
            dpg.add_spacer(height=4)

            # ─── 播放控制 ───
            dpg.add_text("播放与导出", color=C_DIM)
            dpg.add_spacer(height=3)
            cr = dpg.add_group(horizontal=True)
            self._btn_play = dpg.add_button(
                label="  播放  ", callback=lambda: self._play_wav(),
                parent=cr)
            dpg.bind_item_theme(self._btn_play, self._th_play)
            b_stop = dpg.add_button(
                label="  停止  ", callback=lambda: self._stop_wav(),
                parent=cr)
            dpg.bind_item_theme(b_stop, self._th_stop)
            dpg.add_spacer(width=14, parent=cr)
            b_exp = dpg.add_button(
                label="  导出 WAV  ", callback=lambda: self._export(),
                parent=cr)
            dpg.bind_item_theme(b_exp, self._th_sec)
            b_bat = dpg.add_button(
                label="  批量导出  ", callback=lambda: self._batch(),
                parent=cr)
            dpg.bind_item_theme(b_bat, self._th_sec)

            dpg.add_spacer(width=14, parent=cr)
            b_reseed = dpg.add_button(
                label="  换一组噪声  ",
                callback=lambda: (self._new_seed(), self._synth()),
                parent=cr)
            dpg.bind_item_theme(b_reseed, self._th_sec)

            dpg.add_spacer(height=4)
            self._progress = dpg.add_progress_bar(
                default_value=0, width=-1, overlay="")
            dpg.bind_item_theme(self._progress, self._th_prog)
            dpg.hide_item(self._progress)

            dpg.add_spacer(height=6)
            dpg.add_separator()
            dpg.add_spacer(height=4)

            # ─── 波形 ───
            dpg.add_text("波形预览  (蓝=左声道 L  /  橙=右声道 R)", color=C_DIM)
            dpg.add_spacer(height=2)
            with dpg.plot(height=180, width=-1,
                          no_title=True, no_mouse_pos=True):
                self._x_ax = dpg.add_plot_axis(dpg.mvXAxis, label="ms")
                self._y_ax = dpg.add_plot_axis(dpg.mvYAxis, label="")
                self._line_l = dpg.add_line_series(
                    [], [], label="L", parent=self._y_ax)
                dpg.bind_item_theme(self._line_l, self._th_wl)
                self._line_r = dpg.add_line_series(
                    [], [], label="R", parent=self._y_ax)
                dpg.bind_item_theme(self._line_r, self._th_wr)

            dpg.add_spacer(height=6)
            dpg.add_separator()
            dpg.add_spacer(height=4)

            # ─── 预设 ───
            dpg.add_text("预设管理", color=C_DIM)
            dpg.add_spacer(height=3)
            r1 = dpg.add_group(horizontal=True)
            self._inp_name = dpg.add_input_text(
                hint="预设名称", width=160, parent=r1)
            self._inp_note = dpg.add_input_text(
                hint="备注 (选填)", width=200, parent=r1)
            bs = dpg.add_button(
                label="  保存  ", callback=lambda: self._save_preset(),
                parent=r1)
            dpg.bind_item_theme(bs, self._th_sec)
            dpg.add_spacer(height=3)
            r2 = dpg.add_group(horizontal=True)
            self._combo_preset = dpg.add_combo(
                items=[], width=380, parent=r2)
            bl = dpg.add_button(
                label="  加载  ", callback=lambda: self._load_preset(),
                parent=r2)
            dpg.bind_item_theme(bl, self._th_sec)
            bd = dpg.add_button(
                label="  删除  ", callback=lambda: self._del_preset(),
                parent=r2)
            dpg.bind_item_theme(bd, self._th_sec)

            dpg.add_spacer(height=6)
            dpg.add_separator()
            dpg.add_spacer(height=4)
            self._txt_status = dpg.add_text("就绪", color=C_GREEN)

        dpg.bind_theme(self._th_global)
        self._refresh_strat()
        self._refresh_params()
        self._synth()
        self._reload_presets()

    def run(self):
        self.build()
        dpg.create_viewport(title='OSA Sound Designer', width=760, height=1050)
        dpg.setup_dearpygui()
        dpg.set_primary_window('main', True)
        dpg.show_viewport()
        while dpg.is_dearpygui_running():
            self._tick()
            self._check_export_flash()
            dpg.render_dearpygui_frame()
        dpg.destroy_context()

/* OSA experiment console — single-file Vue 3 app (no build step). */

const { createApp } = Vue;

function cls(...xs) { return xs.filter(Boolean).join(' '); }
function fmt(n, d = 1) { return (n === null || n === undefined) ? '—' : Number(n).toFixed(d); }
function secToClock(s) {
  s = Math.max(0, Math.floor(s || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  const mm = String(m).padStart(2, '0'), sss = String(ss).padStart(2, '0');
  return h ? `${h}:${mm}:${sss}` : `${mm}:${sss}`;
}
function postureZh(p) {
  return ({
    supine: '仰卧', prone: '俯卧',
    left: '左侧卧', right: '右侧卧',
    upright: '直立/坐', unknown: '未知',
  })[p] || (p || '—');
}
function controllerStateZh(s) {
  return ({
    idle: '等待条件',
    armed: '仰卧计时中',
    triggered: '已触发',
    playing: '正在播放',
    observe: '观察翻身',
    cooldown: '冷却中',
  })[s] || (s || '—');
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || 'GET',
    headers: opts.body ? { 'Content-Type': 'application/json' } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return await res.json();
}


const App = {
  data() {
    return {
      tab: 'experiment',
      snap: null,
      wsStatus: 'connecting',
      // sound design
      strategies: [],
      strat: 'P1',
      dir: 'left',
      params: {},
      preview: null,
      presets: [],
      presetName: '',
      presetNote: '',
      presetSel: '',
      // devices
      chestScan: { devices: [], busy: false,
                   addr: '', namedOnly: true, chestbandOnly: true },
      audioDevs: { inputs: [], outputs: [], loading: false,
                   inputSel: '', outputSel: '' },
      showChestPanel: true,
      showChestDiag: false,
      showEventTimeline: false,
      // Collapsible sections in the experiment tab.
      // Defaults: monitoring + config open (the live data view), log
      // closed (rarely needed during a run).
      sectionOpen: { mon: true, cfg: true, log: false },
      wakeLockSentinel: null,
      // experiment
      sessionForm: { tag: '', subject: '', note: '' },
      showCtrlCfg: false,
      showSnoreCfg: false,
      yamnetThresh: 0.3,
      modeSwitchBusy: false,
      // replay
      historyList: [],
      replaySelectedId: '',
      replayDetail: null,
      replayActive: null,
      replayLoading: false,
      // charts
      previewChart: null,
      chestChart: null,
      snoreHist: [],
      ticker: 0,
    };
  },
  computed: {
    currentStrategy() {
      return this.strategies.find(s => s.key === this.strat) || null;
    },
    inputDeviceLabel() {
      const idx = this.snap?.audio?.input_device;
      if (idx == null) return 'OS 默认';
      const d = this.audioDevs.inputs.find(x => x.index === idx);
      return d ? `[${idx}] ${d.name}` : `#${idx}`;
    },
    outputDeviceLabel() {
      const idx = this.snap?.audio?.output_device;
      if (idx == null) return 'OS 默认';
      const d = this.audioDevs.outputs.find(x => x.index === idx);
      return d ? `[${idx}] ${d.name}` : `#${idx}`;
    },
    snoreDetail() {
      const s = this.snap?.snore;
      if (!s) return '';
      return `Snoring ${fmt(s.snoring_prob, 2)} · Breathing ${fmt(s.breathing_prob, 2)}` +
        ` · Speech ${fmt(s.speech_prob, 2)} · top=${s.top_class || '—'}`;
    },
    replayChestYMin() {
      const t = this.replayActive?.traces?.chest;
      if (!t || !t.length) return -1;
      return Math.min(...t.map(p => p[1]));
    },
    replayChestYMax() {
      const t = this.replayActive?.traces?.chest;
      if (!t || !t.length) return 1;
      return Math.max(...t.map(p => p[1]));
    },
    snoreView() {
      const W = 1000, H = 200, WINDOW_S = 90;
      const n = this.snoreHist.length;
      if (!n) return null;
      const now = this.snoreHist[n - 1].t;
      const pts = [];
      const segs = [];
      let segStart = null;
      for (const r of this.snoreHist) {
        const dt = r.t - now;
        if (dt < -WINDOW_S) continue;
        const x = ((dt + WINDOW_S) / WINDOW_S) * W;
        const y = H - Math.max(0, Math.min(1, r.p)) * H;
        pts.push([x, y]);
        if (r.snoring && segStart === null) segStart = x;
        if (!r.snoring && segStart !== null) {
          segs.push({ x1: segStart, x2: x });
          segStart = null;
        }
      }
      if (segStart !== null) {
        segs.push({ x1: segStart, x2: pts[pts.length - 1][0] });
      }
      const polyline = pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
      const thr = this.yamnetThresh ?? 0.3;
      const thrY = H - thr * H;
      return { W, H, polyline, segs, thrY, thr, count: pts.length };
    },
    groupedParams() {
      const s = this.currentStrategy;
      if (!s) return [];
      const groups = {};
      for (const p of s.params) {
        (groups[p.group || 'general'] ||= []).push(p);
      }
      const label = { general: '基本参数', spatial: '空间化参数',
                      roughness: '粗糙度调制' };
      return ['general', 'spatial', 'roughness']
        .filter(g => groups[g])
        .map(g => ({ key: g, label: label[g] || g, params: groups[g] }));
    },
    badges() {
      const s = this.snap;
      const mk = (label, state, extra = '') => {
        let kind = '', text = label + '  未连接';
        if (!s) return { label, kind: '', text };
        if (state === 'connected' || state === 'listening') {
          kind = 'ok';
          text = label + '  ' + (extra || '已连接');
        } else if (state === 'connecting') {
          kind = 'warn'; text = label + '  连接中…';
        } else if (state === 'error') {
          kind = 'err';  text = label + '  错误';
        } else {
          kind = ''; text = label + '  未连接';
        }
        return { label, kind, text };
      };
      if (!s) return [];
      const mode = s.experiment?.mode || 'A';
      const isSimple = mode === 'S';
      const out = [
        mk('胸带', s.chestband.state, s.chestband.pkt ? '#' + s.chestband.pkt : ''),
      ];
      if (!isSimple) {
        const spo2Val = s.chestband?.vitals?.spo2;
        const spo2Stale = s.chestband?.spo2_stale;
        let spo2Kind = '', spo2Extra = 'SpO2  —';
        if (spo2Val != null && !spo2Stale) {
          spo2Kind = 'ok'; spo2Extra = `SpO2  ${spo2Val}%`;
        } else if (s.chestband.state === 'connected') {
          spo2Kind = 'warn'; spo2Extra = 'SpO2  失效 (查 PC-68B)';
        }
        out.push({ label: '', kind: spo2Kind, text: spo2Extra });
        out.push(mk('麦克风',
          s.snore.status === 'listening' ? 'listening' : s.snore.status,
          (s.audio?.input_device != null ? `#${s.audio.input_device}` : 'OS 默认') +
          (s.snore.snoring ? ' · 打鼾中' : '')));
      }
      out.push({ label: '输出', kind: '',
        text: '输出  ' + (s.audio?.output_device != null
                          ? '#' + s.audio.output_device : 'OS 默认') });
      return out;
    },
    isSimpleMode() {
      return (this.snap?.experiment?.mode || 'A') === 'S';
    },
    experimentMode() {
      return this.snap?.experiment?.mode || 'A';
    },
    experimentModeLabel() {
      return this.snap?.experiment?.mode_label || '—';
    },
    strategyPool() {
      return this.snap?.controller?.config?.strategy_pool || [];
    },
    strategyRotationMin() {
      return Number(this.snap?.controller?.config?.strategy_rotation_min ?? 0);
    },
    rotationActive() {
      return this.strategyRotationMin > 0 && this.strategyPool.length >= 2;
    },
    rotationState() {
      return this.snap?.controller?.rotation || null;
    },
    upcomingInterventionLabel() {
      const pool = this.strategyPool || [];
      if (this.rotationActive) {
        if (this.rotationState?.current) return this.rotationState.current;
        return '轮播: ' + pool.join(' → ');
      }
      if (pool.length === 1) return pool[0];
      if (pool.length > 1) return '随机抽选: ' + pool.join(' / ');
      return '—';
    },
    strategyPoolLabel() {
      const p = this.strategyPool;
      if (!p.length) return '—';
      if (p.length === 1) return p[0] + ' · 单一固定';
      if (this.rotationActive) {
        return p.join(' → ') + ` · 每 ${fmt(this.strategyRotationMin, 0)} 分钟一轮`;
      }
      return p.join(' / ') + ' · 随机抽选';
    },
    posturePretty() {
      if (!this.snap) return '—';
      const p = this.snap.posture.cls;
      const c = this.snap.posture.conf;
      return postureZh(p) + (c > 0 ? `  (${c.toFixed(2)})` : '');
    },
    triggerArmedPct() {
      if (!this.snap) return 0;
      const c = this.snap.controller;
      if (typeof c.armed_fraction === 'number') return c.armed_fraction;
      const hold = c.config?.trigger_hold_s || c.trigger_hold_s || 8;
      const armed = c.armed_duration || 0;
      return Math.min(1, armed / hold);
    },
    triggerBannerClass() {
      if (!this.snap) return 'bg-slate-800/60 border-slate-600/40';
      const s = this.snap.controller.state;
      const all = this.snap.controller.all_ready;
      if (s === 'triggered' || s === 'playing')
        return 'bg-rose-500/25 border-rose-400 animate-pulse';
      if (s === 'armed')
        return 'bg-emerald-500/20 border-emerald-400';
      if (s === 'cooldown')
        return 'bg-slate-700/60 border-slate-500';
      if (s === 'observe')
        return 'bg-sky-500/20 border-sky-400';
      if (all) return 'bg-emerald-500/10 border-emerald-500/60';
      return 'bg-slate-800/60 border-slate-600/40';
    },
    triggerBannerText() {
      if (!this.snap) return '等待数据…';
      const c = this.snap.controller;
      switch (c.state) {
        case 'triggered': return `已触发! 正在合成 ${c.last_strategy || ''} / ${c.last_direction || ''}`;
        case 'playing':   return `播放中 · ${c.last_strategy || ''} / ${c.last_direction || ''}`;
        case 'observe':   return `观察窗口中 · 等待姿态改变`;
        case 'cooldown': {
          const why = ({ response_success: '成功翻身',
                          no_response: '无反应 → 短冷却后重试',
                          error: '播放失败 → 短冷却' })[c.reason] || c.reason || '';
          return `冷却中 · 还剩 ${fmt(c.cooldown_left, 1)} s${why ? ' · ' + why : ''}`;
        }
        case 'armed': {
          const held = fmt(c.armed_duration, 1);
          const total = fmt(c.trigger_hold_s || c.config?.trigger_hold_s, 1);
          const tag = c.retry_mode ? ' · 重试模式 (快节奏)' : '';
          const need = c.confirm_snore_bouts ?? 0;
          const got = c.snore_bouts_since_armed ?? 0;
          // Retry mode skips the confirmation check (we already know they
          // are snoring + supine), so don't show the counter then.
          const conf = (c.config?.require_snoring && need > 0 && !c.retry_mode)
            ? ` · 鼾声确认 ${got}/${need}`
            : '';
          return `条件满足中 · ${held} / ${total} s${conf}${tag}`;
        }
        default:
          return c.all_ready
            ? '条件全部满足 · 等待姿态事件唤醒控制器'
            : '未满足触发条件 · 见下方清单';
      }
    },
    isConnectedChest() { return this.snap?.chestband?.state === 'connected'; },
    micLevelPct() {
      const db = this.snap?.snore?.energy_db ?? -80;
      const pct = ((db + 80) / 60) * 100;
      return Math.max(0, Math.min(100, pct));
    },
  },
  async mounted() {
    try {
      this.strategies = await api('/api/strategies');
      this.resetParamsFromStrategy();
      await this.reloadPresets();
      await this.reloadAudioDevices();
    } catch (e) { console.error(e); }
    this.connectWS();
    document.addEventListener('visibilitychange', this._onVisibility);
    this.$nextTick(() => {
      this.buildPreviewChart();
      this.buildChestChart();
      this.refreshPreview();
    });
  },
  beforeUnmount() {
    if (this.ws) try { this.ws.close(); } catch {}
    if (this._audioPoll) clearInterval(this._audioPoll);
    document.removeEventListener('visibilitychange', this._onVisibility);
    this.releaseWakeLock();
  },
  watch: {
    strat() { this.resetParamsFromStrategy(); this.refreshPreview(); },
    dir() { this.refreshPreview(); },
    tab(v) {
      if (this._audioPoll) { clearInterval(this._audioPoll); this._audioPoll = null; }
      if (v === 'devices') {
        this.reloadAudioDevices();
        this._audioPoll = setInterval(() => this.reloadAudioDevices(), 8000);
      }
    },
  },
  methods: {
    controllerStateZh,
    connectWS() {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const url = `${proto}://${location.host}/ws`;
      this.wsStatus = 'connecting';
      const ws = new WebSocket(url);
      this.ws = ws;
      ws.onopen  = () => {
        this.wsStatus = 'open';
        this._wsRetry = 0;  // reset backoff on successful connect
      };
      ws.onclose = () => {
        this.wsStatus = 'closed';
        // Exponential backoff: 1s, 2s, 4s, 8s, 15s (capped) — keeps us
        // gentle when the server is restarting but recovers fast normally.
        const n = (this._wsRetry || 0) + 1;
        this._wsRetry = n;
        const delay = Math.min(15000, 1000 * Math.pow(2, Math.min(n - 1, 4)));
        setTimeout(() => this.connectWS(), delay);
      };
      ws.onerror = () => { this.wsStatus = 'error'; };
      ws.onmessage = (ev) => {
        try {
          const snap = JSON.parse(ev.data);
          this.snap = snap;
          this.ticker++;
          this.pushSnoreSample(snap);
          this.updateChestChart();
        } catch {}
      };
    },

    // ── sound design ────────────────────────────────────────
    resetParamsFromStrategy() {
      const s = this.currentStrategy;
      if (!s) return;
      const p = {};
      for (const ps of s.params) p[ps.key] = ps.default;
      this.params = p;
      if (!s.has_direction) this.dir = 'center';
      else if (this.dir === 'center') this.dir = 'left';
    },
    async refreshPreview() {
      if (!this.currentStrategy) return;
      try {
        const r = await api('/api/preview', { method: 'POST', body: {
          strategy: this.strat, direction: this.dir, params: this.params,
        }});
        if (r.ok) {
          this.preview = r;
          this.updatePreviewChart();
        }
      } catch (e) { console.error(e); }
    },
    onParamChange() { this.refreshPreview(); },
    async playStrategy() {
      await api('/api/play', { method: 'POST', body: {
        strategy: this.strat, direction: this.dir, params: this.params,
        repeats: 3,
      }});
    },
    async stopAudio() { await api('/api/stop', { method: 'POST' }); },
    async exportOne() {
      const r = await api('/api/export', { method: 'POST', body: {
        strategy: this.strat, direction: this.dir, params: this.params,
      }});
      if (r.ok) alert(`已导出  ${r.name}`);
    },
    async batchExport() {
      const r = await api('/api/batch-export', { method: 'POST' });
      if (r.ok) alert(`批量导出完成, ${r.count} 个文件 → output/`);
    },

    // ── presets ────────────────────────────────────────────
    async reloadPresets() { this.presets = await api('/api/presets'); },
    presetDisp(p) {
      return `[${p.strategy}] ${p.name}${p.note ? ' · ' + p.note : ''}`;
    },
    async savePreset() {
      const name = (this.presetName || '').trim();
      if (!name) { alert('请输入预设名'); return; }
      const r = await api('/api/presets', { method: 'POST', body: {
        name, strategy: this.strat, direction: this.dir,
        params: this.params, note: this.presetNote || '',
      }});
      if (r.ok) {
        this.presetName = ''; this.presetNote = '';
        await this.reloadPresets();
      } else alert('保存失败: ' + (r.err || ''));
    },
    async loadPreset() {
      const p = this.presets.find(x => this.presetDisp(x) === this.presetSel);
      if (!p) return;
      this.strat = p.strategy;
      this.$nextTick(() => {
        const next = {};
        for (const ps of (this.currentStrategy?.params || [])) {
          next[ps.key] = (p.params && p.params[ps.key] !== undefined)
                          ? p.params[ps.key] : ps.default;
        }
        this.params = next;
        if (this.currentStrategy?.has_direction && p.direction) {
          this.dir = p.direction;
        }
        this.refreshPreview();
      });
    },
    async delPreset() {
      const p = this.presets.find(x => this.presetDisp(x) === this.presetSel);
      if (!p) return;
      if (!confirm('删除预设 ' + p.name + ' ?')) return;
      await api('/api/presets', { method: 'DELETE', body: { name: p.name } });
      await this.reloadPresets();
    },

    // ── devices ────────────────────────────────────────────
    async chestScanGo() {
      this.chestScan.busy = true;
      try {
        const r = await api('/api/ble/chest/scan', { method: 'POST', body: {
          named_only: this.chestScan.namedOnly,
          chestband_only: this.chestScan.chestbandOnly,
        }});
        if (r.ok) this.chestScan.devices = r.devices;
      } finally { this.chestScan.busy = false; }
    },
    async chestConnectGo() {
      if (!this.chestScan.addr) { alert('请选设备'); return; }
      await api('/api/ble/chest/connect', { method: 'POST', body: {
        address: this.chestScan.addr }});
    },
    async chestDisconnectGo() {
      await api('/api/ble/chest/disconnect', { method: 'POST' });
    },
    async chestSilenceSend(body, label) {
      // body: { keep_spo2?: bool, switches?: int }
      // Always shows result so we know whether the frame got through
      // even when the chest band's beep didn't actually stop.
      try {
        const r = await api('/api/ble/chest/silence', { method: 'POST',
          body });
        const lines = [
          `${label || '0x0B 告警开关帧'} → 写入 ${r.ok ? '成功' : '失败'}`,
          `switches = ${r.switches_hex || ('0x' + (r.switches ?? 0).toString(16).padStart(2, '0').toUpperCase())}`,
          `DID = ${r.did_hex || '—'}`,
          `frame = ${r.frame_hex || '—'}`,
        ];
        if (r.err) lines.push('错误: ' + r.err);
        if (r.ok) {
          lines.push('');
          lines.push('如果蜂鸣声没停, 说明这版固件不响应这组开关位 ─');
          lines.push('换个 switches 值再试 (按钮旁边「试其他位」).');
        }
        alert(lines.join('\n'));
      } catch (e) {
        alert('请求失败: ' + e);
      }
    },
    chestSilenceGo() {
      // Send 0x0B silence frame (bit 6 = keep BT pulse-oximeter relay).
      // Empirically ineffective on the HSRG firmware variant — kept here
      // as a fallback in the diagnostics panel; the working command is
      // `chestResyncRTC`.
      this.chestSilenceSend({ keep_spo2: true }, '0x0B 状态控制帧');
    },
    async chestUnbindAll() {
      try {
        const r = await api('/api/ble/chest/unbind_all',
          { method: 'POST', body: {} });
        const lines = [`解绑全部外设 → ${r.ok ? '部分/全部成功' : '失败'}`];
        for (const x of (r.results || [])) {
          lines.push(` · ${x.label} (type=0x${x.type.toString(16).padStart(2,'0')}) ${x.ok ? 'OK' : '失败'}`);
        }
        lines.push('');
        lines.push('观察「告警标志位」横幅: bit 2-5 应该会清零.');
        lines.push('如果 bit 0 (心电脱落) 还在 → 是电极阻抗问题, 抹点导电凝胶/盐水.');
        alert(lines.join('\n'));
      } catch (e) { alert('请求失败: ' + e); }
    },
    async chestResyncRTC() {
      try {
        const r = await api('/api/ble/chest/resync_rtc',
          { method: 'POST', body: {} });
        alert(`重发 RTC 同步 → ${r.ok ? '成功' : '失败'}\n`
          + `frame = ${r.frame_hex || '—'}\n`
          + (r.err ? '错误: ' + r.err : '')
          + '\n下一秒看 device_status, bit 6 应该清零.');
      } catch (e) { alert('请求失败: ' + e); }
    },
    async testTone(d) {
      const r = await api('/api/play', { method: 'POST', body: {
        strategy: 'P1', direction: d, params: {}, repeats: 1 }});
      if (!r.ok) alert('播放失败: ' + (r.err || '未知'));
    },

    // ── audio device picker ───────────────────────────────
    async reloadAudioDevices() {
      this.audioDevs.loading = true;
      try {
        const r = await api('/api/audio/devices');
        if (r.ok) {
          this.audioDevs.inputs  = r.inputs;
          this.audioDevs.outputs = r.outputs;
          this.audioDevs.inputSel  = r.selected_input  ?? '';
          this.audioDevs.outputSel = r.selected_output ?? '';
        }
      } finally { this.audioDevs.loading = false; }
    },
    async applyAudioInput() {
      const v = this.audioDevs.inputSel;
      const idx = (v === '' || v === null) ? null : parseInt(v, 10);
      const r = await api('/api/audio/devices', { method: 'POST', body: {
        set_input: true, input_device: Number.isNaN(idx) ? null : idx,
      }});
      if (!r.ok) alert('切换输入设备失败: ' + (r.err || ''));
    },
    async applyAudioOutput() {
      const v = this.audioDevs.outputSel;
      const idx = (v === '' || v === null) ? null : parseInt(v, 10);
      const r = await api('/api/audio/devices', { method: 'POST', body: {
        set_output: true, output_device: Number.isNaN(idx) ? null : idx,
      }});
      if (!r.ok) alert('切换输出设备失败: ' + (r.err || ''));
    },

    // ── experiment ─────────────────────────────────────────
    async sessionStart() {
      const subject = (this.sessionForm.subject || '').trim();
      if (!subject) {
        alert('请先填写被试 ID/姓名');
        return;
      }
      const r = await api('/api/session/start', { method: 'POST', body: {
        tag: this.sessionForm.tag || '',
        subject,
        note: this.sessionForm.note || '',
      }});
      if (!r.ok) {
        alert('开始失败: ' + (r.err || ''));
        return;
      }
      this.acquireWakeLock();
    },
    async sessionStop() {
      await api('/api/session/stop', { method: 'POST' });
      this.releaseWakeLock();
    },
    async acquireWakeLock() {
      // Best-effort screen wake lock so the phone / laptop doesn't sleep
      // while a session is running. Browser support varies, so failures
      // are silently ignored — the OS-level safety net (e.g. caffeinate
      // on macOS) handles the rest.
      if (!('wakeLock' in navigator)) return;
      try {
        this.wakeLockSentinel = await navigator.wakeLock.request('screen');
        this.wakeLockSentinel.addEventListener('release', () => {
          // OS or another tab released our lock. If a session is still
          // running, try to re-acquire on next visibility event.
          this.wakeLockSentinel = null;
        });
      } catch (e) {
        console.warn('wakeLock 请求失败:', e);
      }
    },
    releaseWakeLock() {
      if (this.wakeLockSentinel) {
        try { this.wakeLockSentinel.release(); } catch {}
        this.wakeLockSentinel = null;
      }
    },
    _onVisibility() {
      // When the page becomes visible again (e.g. user unlocked phone),
      // re-acquire the wake lock if a session is still running. Browsers
      // automatically release wake locks when the page goes to background.
      if (document.visibilityState === 'visible'
          && this.snap?.session?.active
          && !this.wakeLockSentinel) {
        this.acquireWakeLock();
      }
    },
    async manualTrigger() { await api('/api/trigger', { method: 'POST' }); },
    async switchExperimentMode(target) {
      if (this.modeSwitchBusy) return;
      if (this.snap?.session?.active) {
        alert('会话进行中, 请先「结束会话」再切换实验模式');
        return;
      }
      if (target === this.experimentMode) return;
      const tip = target === 'S'
        ? '切到「S · 仅姿态」模式? 将停止麦克风采集, 触发条件简化为仅仰卧'
        : '切到「A · 仰卧+鼾声 (Block A)」模式? 将重启麦克风+YAMNet';
      if (!confirm(tip)) return;
      this.modeSwitchBusy = true;
      try {
        const r = await api('/api/experiment/mode', { method: 'POST',
          body: { mode: target }});
        if (!r.ok) alert('切换失败: ' + (r.err || '未知'));
      } finally { this.modeSwitchBusy = false; }
    },
    async openSessionsDir() {
      await api('/api/history/open', { method: 'POST' });
    },
    async applyCtrlCfg(patch) {
      await api('/api/controller/config', { method: 'POST', body: patch });
    },
    async setSingleStrategy(key) {
      const r = await api('/api/controller/config', { method: 'POST',
        body: { strategy_pool: [key], strategy_rotation_min: 0 }});
      if (!r.ok) alert('切换策略失败: ' + (r.err || ''));
    },
    async setRotationMinutes(min) {
      const v = Math.max(0, Number(min) || 0);
      const r = await api('/api/controller/config', { method: 'POST',
        body: { strategy_rotation_min: v }});
      if (!r.ok) alert('设置轮播间隔失败: ' + (r.err || ''));
    },
    async togglePoolStrategy(key) {
      const cur = this.strategyPool.slice();
      const idx = cur.indexOf(key);
      if (idx >= 0) {
        if (cur.length <= 1) {
          alert('至少需要保留 1 个策略');
          return;
        }
        cur.splice(idx, 1);
      } else {
        cur.push(key);
      }
      const r = await api('/api/controller/config', { method: 'POST',
        body: { strategy_pool: cur }});
      if (!r.ok) alert('修改策略池失败: ' + (r.err || ''));
    },
    async applySnoreCfg(patch) {
      await api('/api/snore/config', { method: 'POST', body: patch });
    },

    // ── replay ──────────────────────────────────────────
    async reloadHistoryList() {
      try { this.historyList = await api('/api/history?limit=20'); }
      catch (e) { console.error(e); }
    },
    async openReplay(sessionId) {
      this.replaySelectedId = sessionId;
      this.replayActive = null;
      this.replayLoading = true;
      try {
        this.replayDetail = await api('/api/history/' + sessionId);
      } finally { this.replayLoading = false; }
    },
    async runReplayAnalysis() {
      if (!this.replaySelectedId) return;
      this.replayLoading = true;
      try {
        const r = await api(
          '/api/history/' + this.replaySelectedId + '/analyze',
          { method: 'POST' });
        if (r.ok) this.replayDetail = r;
        else alert('分析失败: ' + (r.err || ''));
      } finally { this.replayLoading = false; }
    },
    async openReplayEvent(evt) {
      if (!this.replayDetail) return;
      this.replayLoading = true;
      try {
        const url = '/api/history/' + this.replayDetail.id +
                    '/event_detail?base=' + encodeURIComponent(evt.base);
        const traces = await api(url);
        this.replayActive = { event: evt, traces };
      } catch (e) {
        alert('载入事件失败: ' + e);
      } finally { this.replayLoading = false; }
    },
    replayTraceView(series, opts) {
      const W = opts.w, H = opts.h;
      const tmin = opts.tmin ?? -60, tmax = opts.tmax ?? 30;
      const ymin = opts.ymin, ymax = opts.ymax;
      if (!series || !series.length) return '';
      const pts = [];
      for (const [t, y] of series) {
        if (t < tmin || t > tmax) continue;
        const x = ((t - tmin) / (tmax - tmin)) * W;
        const yy = H - ((y - ymin) / (ymax - ymin)) * H;
        if (isFinite(yy)) pts.push(x.toFixed(1) + ',' + yy.toFixed(1));
      }
      return pts.join(' ');
    },

    // ── charts ────────────────────────────────────────────
    buildPreviewChart() {
      const ctx = document.getElementById('c-preview');
      if (!ctx) return;
      this.previewChart = new Chart(ctx.getContext('2d'), {
        type: 'line',
        data: {
          labels: [],
          datasets: [
            { label: 'L', data: [], borderColor: '#38bdf8',
              borderWidth: 1.2, pointRadius: 0, tension: 0 },
            { label: 'R', data: [], borderColor: '#f59e0b',
              borderWidth: 1.2, pointRadius: 0, tension: 0 },
          ],
        },
        options: {
          animation: false, responsive: true, maintainAspectRatio: false,
          plugins: { legend: { labels: { color: '#cbd5e1' }}},
          scales: {
            x: { ticks: { color: '#64748b' },
                 grid: { color: 'rgba(148,163,184,0.08)' },
                 title: { display: true, text: 'ms', color: '#64748b' }},
            y: { ticks: { color: '#64748b' },
                 grid: { color: 'rgba(148,163,184,0.08)' }},
          },
        },
      });
    },
    updatePreviewChart() {
      if (!this.previewChart || !this.preview) return;
      const p = this.preview;
      this.previewChart.data.labels = p.t_ms.map(x => x.toFixed(0));
      this.previewChart.data.datasets[0].data = p.L;
      this.previewChart.data.datasets[1].data = p.R;
      this.previewChart.update('none');
    },
    buildChestChart() {
      const ctx = document.getElementById('c-chest');
      if (!ctx) return;
      this.chestChart = new Chart(ctx.getContext('2d'), {
        type: 'line',
        data: { labels: [], datasets: [{
          label: '胸呼吸', data: [], borderColor: '#38bdf8',
          borderWidth: 1.4, pointRadius: 0, tension: 0.25, fill: false,
        }]},
        options: {
          animation: false, responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false }},
          scales: {
            x: {
              type: 'linear',
              min: 0, max: 30,
              title: { display: true, text: '秒 (近 30 秒)', color: '#64748b' },
              ticks: { color: '#64748b', stepSize: 5 },
              grid: { color: 'rgba(148,163,184,0.08)' },
            },
            y: {
              title: { display: true, text: '胸呼吸幅度', color: '#64748b' },
              ticks: { color: '#64748b' },
              grid: { color: 'rgba(148,163,184,0.08)' },
            },
          },
        },
      });
    },
    pushSnoreSample(snap) {
      if (!snap || !snap.snore) return;
      const t = snap.t || (Date.now() / 1000);
      const p = snap.snore.snoring_prob ?? 0;
      const row = { t, p: +p, snoring: !!snap.snore.snoring };
      this.snoreHist.push(row);
      if (this.snoreHist.length > 360) this.snoreHist.shift();
    },

    updateChestChart() {
      if (!this.chestChart || !this.snap) return;
      const cb = this.snap.chestband;
      const pts = (cb.chest_t || []).map((t, i) => ({ x: t, y: cb.chest_y[i] }));
      this.chestChart.data.datasets[0].data = pts;
      const xmax = Math.max(30, pts.length ? pts[pts.length - 1].x : 30);
      this.chestChart.options.scales.x.min = Math.max(0, xmax - 30);
      this.chestChart.options.scales.x.max = xmax;
      this.chestChart.update('none');
    },
  },

  // ── render ──────────────────────────────────────────────
  template: /*html*/ `
<div class="min-h-screen flex flex-col">

  <header class="sticky top-0 z-30 backdrop-blur
                 bg-ink-900/80 border-b border-slate-700/40">
    <div class="max-w-[1200px] mx-auto px-4 py-3">
      <!-- Row 1: logo + WS status (small screen: condensed) -->
      <div class="flex items-center gap-3">
        <div class="flex items-center gap-2">
          <div class="w-8 h-8 rounded-xl bg-gradient-to-br
                      from-sky-400 to-indigo-500 grid place-items-center
                      shadow shadow-sky-500/30">
            <span class="text-xs font-bold text-white">OSA</span>
          </div>
          <div class="leading-tight">
            <div class="font-semibold text-sm sm:text-base">打鼾干预实验工作台</div>
            <div class="text-[10px] sm:text-xs dim flex items-center gap-1.5">
              <span class="inline-block w-1.5 h-1.5 rounded-full"
                    :class="wsStatus === 'open' ? 'bg-emerald-400'
                          : wsStatus === 'connecting' ? 'bg-amber-400 animate-pulse'
                          : 'bg-rose-500'"></span>
              <span class="hidden sm:inline">WebSocket · {{ wsStatus }}</span>
              <span class="sm:hidden">{{ wsStatus }}</span>
            </div>
          </div>
        </div>
        <div class="flex-1"></div>
        <!-- Status badges: visible on tablet+, hidden on phone (详情在主体卡片) -->
        <div class="hidden md:flex gap-2 flex-wrap">
          <span v-for="b in badges" :key="b.label"
                :class="['badge', b.kind]">
            <span class="dot"></span>{{ b.text }}
          </span>
        </div>
      </div>

      <!-- Row 2: tab nav + mode switch (horizontal scroll on small screen) -->
      <div class="mt-3 -mx-4 px-4 overflow-x-auto">
        <div class="flex items-center gap-2 min-w-max">
          <nav class="flex gap-1 p-1 rounded-xl bg-slate-800/40 border border-slate-700/40">
            <button class="tab-btn" :class="{active: tab==='experiment'}"
                    @click="tab='experiment'">实时实验</button>
            <button class="tab-btn" :class="{active: tab==='devices'}"
                    @click="tab='devices'">设备</button>
            <button class="tab-btn" :class="{active: tab==='replay'}"
                    @click="tab='replay'">回放</button>
            <button class="tab-btn text-xs"
                    :class="{active: tab==='design'}"
                    @click="tab='design'"
                    title="声音策略调试 (开发用, 实验时可忽略)">⚙</button>
          </nav>
          <div class="flex items-center gap-1 p-1 rounded-xl
                      bg-slate-800/40 border border-slate-700/40"
               v-if="snap" :title="'实验模式 · ' + experimentModeLabel">
            <button class="tab-btn" :class="{active: experimentMode==='A'}"
                    :disabled="modeSwitchBusy || (snap?.session?.active && experimentMode!=='A')"
                    @click="switchExperimentMode('A')">A · 仰卧+鼾声</button>
            <button class="tab-btn" :class="{active: experimentMode==='S'}"
                    :disabled="modeSwitchBusy || (snap?.session?.active && experimentMode!=='S')"
                    @click="switchExperimentMode('S')">S · 仅姿态</button>
          </div>
        </div>
      </div>

      <!-- Row 3: session controls (full-width input on phone) -->
      <div class="mt-3 flex items-center gap-2 flex-wrap">
        <template v-if="!snap || !snap.session.active">
          <input type="text" v-model="sessionForm.subject"
                 placeholder="被试 ID / 姓名"
                 class="flex-1 min-w-[140px] sm:flex-initial sm:w-[200px]">
          <button class="btn success" @click="sessionStart">开始会话</button>
        </template>
        <template v-else>
          <div class="text-sm flex-1 min-w-0">
            <span class="dim">会话</span>
            <span class="font-mono ml-1 truncate inline-block max-w-[160px] align-bottom"
                  :title="snap.session.id">{{ snap.session.id }}</span>
            <span class="text-emerald-300 ml-2">·
              {{ secToClock(snap.session.duration_s) }}</span>
            <span v-if="snap.session.subject" class="dim ml-2">·
              {{ snap.session.subject }}</span>
          </div>
          <button class="btn danger" @click="sessionStop">结束会话</button>
        </template>
        <button class="btn warn" @click="manualTrigger">手动触发</button>
      </div>

      <div v-if="snap" class="mt-3 rounded-xl border px-4 py-3 transition"
           :class="triggerBannerClass">
        <div class="flex items-center gap-3 flex-wrap">
          <div class="font-semibold">
            <span class="mr-2">状态:</span>
            <span class="kbd">{{ snap.controller.state || 'idle' }}</span>
          </div>
          <div class="text-sm flex-1 min-w-[220px]">{{ triggerBannerText }}</div>
          <div class="flex gap-2 flex-wrap">
            <span v-for="c in (snap.controller.conditions || [])"
                  :key="c.key"
                  :class="['badge', c.ok ? 'ok' : '']">
              <span class="dot"></span>
              {{ c.label }}
              <span v-if="!c.ok && c.hint" class="dim ml-1">· {{ c.hint }}</span>
            </span>
          </div>
        </div>
        <div v-if="snap.controller.state === 'armed'" class="mt-2">
          <div class="progress trigger"><div
            :style="{width: (triggerArmedPct*100).toFixed(1) + '%'}"></div></div>
        </div>
        <div v-if="snap.last_error"
             class="mt-2 text-rose-200 text-sm flex items-center gap-2">
          <span class="badge err"><span class="dot"></span>错误</span>
          {{ snap.last_error.msg }}
        </div>
      </div>
    </div>
  </header>

  <main class="flex-1 w-full max-w-[1200px] mx-auto px-4 py-6 space-y-6">

    <!-- ====== tab: 实时实验 ====== -->
    <section v-show="tab==='experiment'" class="space-y-6">
      <div v-if="isSimpleMode"
           class="rounded-xl border border-amber-500/40 bg-amber-500/10
                  px-4 py-3 text-sm">
        <div class="font-semibold text-amber-200">
          S 模式 · 仅姿态干预 (简化版)
        </div>
        <div class="text-amber-100/80 mt-1">
          硬件: 仅胸带 + 耳机. 触发条件: 仰卧持续
          <span class="kbd">{{ fmt(snap?.controller?.config?.trigger_hold_s, 0) }} s</span>
          → 播放
          <span v-if="rotationActive" class="kbd">
            轮播 {{ strategyPool.join('→') }} (每 {{ fmt(strategyRotationMin, 0) }} 分钟)
          </span>
          <span v-else class="kbd">{{ strategyPool.join(' / ') || '—' }}</span>
          → 观察
          <span class="kbd">{{ fmt(snap?.controller?.config?.response_window_s, 0) }} s</span>
          内是否翻身. 不需要 PC-68B 血氧仪、不需要麦克风/YAMNet.
        </div>
      </div>

      <!-- ═══ 📡 实时监控 ═══ -->
      <div class="section-bar mon" @click="sectionOpen.mon = !sectionOpen.mon">
        <span class="chevron" :class="{ open: sectionOpen.mon }">▶</span>
        📡 实时监控
        <span class="count">
          <span v-if="snap?.posture?.cls && snap.posture.cls !== 'unknown'">
            {{ posturePretty }}</span>
          <span v-if="!isSimpleMode && snap?.chestband?.vitals?.spo2">
            · SpO2 {{ snap.chestband.vitals.spo2 }}%</span>
          <span v-if="!isSimpleMode && snap?.chestband?.chest_rr">
            · RR {{ fmt(snap.chestband.chest_rr, 0) }}</span>
        </span>
      </div>

      <div v-show="sectionOpen.mon" class="space-y-6">

      <div :class="isSimpleMode
                  ? 'grid grid-cols-1 md:grid-cols-2 gap-4'
                  : 'grid grid-cols-2 md:grid-cols-4 gap-3'">
        <div class="card mon p-4 text-center"
             v-if="!isSimpleMode"
             :class="{ 'opacity-40': isSimpleMode || snap?.chestband?.spo2_stale }">
          <div class="text-xs dim">SpO2</div>
          <div class="mt-1 text-3xl font-semibold font-mono">
            <span v-if="!isSimpleMode && snap?.chestband?.vitals?.spo2">
              {{ snap.chestband.vitals.spo2 }}<span class="text-base dim">%</span>
            </span>
            <span v-else class="dim">—</span>
          </div>
          <div class="mt-1 text-[10px] dim">
            <span v-if="isSimpleMode">S 模式 · 未启用</span>
            <span v-else-if="snap?.chestband?.spo2_stale">失效 (查 PC-68B)</span>
            <span v-else>来源: PC-68B 转发</span>
          </div>
        </div>
        <div class="card mon p-4 text-center"
             v-if="!isSimpleMode"
             :class="{ 'opacity-40': isSimpleMode || snap?.chestband?.pulse_stale }">
          <div class="text-xs dim">脉率</div>
          <div class="mt-1 text-3xl font-semibold font-mono">
            <span v-if="!isSimpleMode && snap?.chestband?.vitals?.pulse">
              {{ snap.chestband.vitals.pulse }}<span class="text-base dim"> bpm</span>
            </span>
            <span v-else class="dim">—</span>
          </div>
          <div class="mt-1 text-[10px] dim">
            <span v-if="isSimpleMode">S 模式 · 未启用</span>
            <span v-else>PC-68B 脉搏波</span>
          </div>
        </div>
        <div class="card mon p-4 text-center" v-if="!isSimpleMode">
          <div class="text-xs dim">呼吸率</div>
          <div class="mt-1 text-3xl font-semibold font-mono">
            <span v-if="snap?.chestband?.chest_rr">
              {{ fmt(snap.chestband.chest_rr, 1) }}<span class="text-base dim"> /min</span>
            </span>
            <span v-else class="dim">—</span>
          </div>
          <div class="mt-1 text-[10px] dim">胸带 RIP 峰检测</div>
        </div>
        <div class="card mon p-5 text-center">
          <div class="text-xs dim">姿态</div>
          <div :class="isSimpleMode ? 'mt-2 text-3xl font-semibold' : 'mt-1 text-2xl font-semibold'">
            <span v-if="snap?.posture?.cls && snap.posture.cls !== 'unknown'">
              {{ posturePretty }}</span>
            <span v-else class="dim">—</span>
          </div>
          <div class="mt-1 text-[10px] dim">
            胸带 accel · debounce {{ fmt(snap?.controller?.config?.debounce_s, 1) }} s
          </div>
        </div>
        <div class="card mon p-5 text-center" v-if="isSimpleMode">
          <div class="text-xs dim">干预声</div>
          <div class="mt-2 text-2xl font-semibold">
            {{ controllerStateZh(snap?.controller?.state) }}
          </div>
          <div class="mt-3 rounded-lg border border-sky-500/25 bg-sky-500/10 p-3">
            <div class="text-xs dim">下一次触发将使用</div>
            <div class="mt-1 text-lg font-semibold font-mono text-sky-200">
              {{ upcomingInterventionLabel }}
            </div>
          </div>
          <div class="mt-2 text-xs dim">
            <span v-if="snap?.controller?.state === 'playing' || snap?.controller?.state === 'triggered'">
              正在播放 {{ snap.controller.last_strategy || '—' }}
              <span v-if="snap.controller.last_direction"> · {{ snap.controller.last_direction }}</span>
            </span>
            <span v-else-if="snap?.controller?.state === 'cooldown'">
              剩余 {{ fmt(snap.controller.cooldown_left, 1) }} s
            </span>
            <span v-else-if="snap?.controller?.state === 'armed'">
              {{ fmt(snap.controller.armed_duration, 1) }} /
              {{ fmt(snap.controller.trigger_hold_s, 1) }} s
            </span>
            <span v-else>
              最近 {{ snap?.controller?.last_strategy || '尚未播放' }}
            </span>
          </div>
          <div v-if="snap?.controller?.state === 'armed'" class="mt-2">
            <div class="progress trigger"><div
              :style="{width: (triggerArmedPct*100).toFixed(1) + '%'}"></div></div>
          </div>
        </div>
      </div>

      <div class="card p-5 mon" v-if="!isSimpleMode">
        <div class="flex items-center justify-between flex-wrap gap-2">
          <h3 class="text-sky-300">鼾声判决时间线  (近 90 秒)</h3>
          <div class="text-xs dim font-mono flex items-center gap-3 flex-wrap" v-if="snap">
            <span>Snore <span class="text-sky-300">{{
              fmt(snap.snore?.snoring_prob ?? 0, 2) }}</span></span>
            <span>Breath {{ fmt(snap.snore?.breathing_prob ?? 0, 2) }}</span>
            <span>Speech {{ fmt(snap.snore?.speech_prob ?? 0, 2) }}</span>
            <span>top=<span class="text-emerald-300">{{
              snap.snore?.top_class || '—' }}</span></span>
            <span :class="snap.snore?.snoring ? 'text-emerald-300' : 'dim'">
              {{ snap.snore?.snoring ? '● 判为打鼾' : '○ 未触发' }}</span>
          </div>
        </div>
        <div class="mt-3 flex items-center gap-2 text-xs dim font-mono" v-if="snap">
          <span class="w-12">麦克</span>
          <div class="flex-1 h-2 rounded-full bg-slate-800/80 overflow-hidden">
            <div class="h-full bg-emerald-400 transition-[width] duration-200"
                 :style="{ width: micLevelPct + '%' }"></div>
          </div>
          <span class="w-24 text-right">{{ fmt(snap.snore?.energy_db ?? -80, 1) }} dB</span>
        </div>
        <div class="mt-3 relative">
          <svg v-if="snoreView" :viewBox="'0 0 ' + snoreView.W + ' ' + snoreView.H"
               preserveAspectRatio="none" class="w-full h-[220px]"
               style="background: rgba(15,23,42,0.45); border-radius: 10px;">
            <g stroke="rgba(148,163,184,0.12)" stroke-width="1">
              <line v-for="i in 5" :key="'h'+i"
                    :x1="0" :x2="snoreView.W"
                    :y1="(snoreView.H * i / 5)" :y2="(snoreView.H * i / 5)"/>
              <line v-for="i in 9" :key="'v'+i"
                    :x1="(snoreView.W * i / 9)" :x2="(snoreView.W * i / 9)"
                    :y1="0" :y2="snoreView.H"/>
            </g>
            <g fill="rgba(34,197,94,0.30)" stroke="none">
              <rect v-for="(s, i) in snoreView.segs" :key="'seg'+i"
                    :x="s.x1" :y="0"
                    :width="Math.max(1, s.x2 - s.x1)" :height="snoreView.H"/>
            </g>
            <line :x1="0" :x2="snoreView.W"
                  :y1="snoreView.thrY" :y2="snoreView.thrY"
                  stroke="#f59e0b" stroke-dasharray="6 4" stroke-width="1"/>
            <text :x="snoreView.W - 6" :y="snoreView.thrY - 4"
                  text-anchor="end" fill="#f59e0b" font-size="12"
                  font-family="ui-monospace, Menlo, monospace">
              阈值 {{ fmt(snoreView.thr, 2) }}
            </text>
            <polyline :points="snoreView.polyline"
                      fill="none" stroke="#38bdf8" stroke-width="1.8"
                      vector-effect="non-scaling-stroke"/>
            <g fill="#64748b" font-size="11"
               font-family="ui-monospace, Menlo, monospace">
              <text :x="4" :y="12">1.0</text>
              <text :x="4" :y="snoreView.H / 2 + 4">0.5</text>
              <text :x="4" :y="snoreView.H - 4">0.0</text>
              <text :x="4" :y="snoreView.H - 4 - 14" fill="#94a3b8">-90s</text>
              <text :x="snoreView.W - 30" :y="snoreView.H - 4 - 14"
                    fill="#94a3b8">现在</text>
            </g>
          </svg>
          <div v-else
               class="h-[220px] flex items-center justify-center
                      text-sm dim rounded-xl"
               style="background: rgba(15,23,42,0.45);">
            (等待 WebSocket 数据...)
          </div>
        </div>
      </div>

      <div class="card mon p-5" v-show="!isSimpleMode">
        <div class="flex items-center justify-between flex-wrap gap-2">
          <h3 class="text-sky-300">胸呼吸  (近 30 秒)</h3>
          <div class="text-xs dim" v-if="snap">
            呼吸率 {{ snap.chestband.chest_rr
                        ? fmt(snap.chestband.chest_rr, 1) + ' /min' : '—' }}
          </div>
        </div>
        <div class="mt-3 relative h-[220px]">
          <canvas id="c-chest"></canvas>
          <div v-if="snap && (!snap.chestband.chest_y || !snap.chestband.chest_y.length)"
               class="absolute inset-0 flex items-center justify-center
                      text-sm dim pointer-events-none">
            (胸带未连接或尚无数据 — 连接后自动显示波形)
          </div>
        </div>
      </div>

      </div><!-- end mon section -->

      <!-- ═══ ⚙ 实验配置 ═══ -->
      <div class="section-bar cfg" @click="sectionOpen.cfg = !sectionOpen.cfg">
        <span class="chevron" :class="{ open: sectionOpen.cfg }">▶</span>
        ⚙ 实验配置
        <span class="count">
          <span class="kbd">{{ strategyPoolLabel }}</span>
          <span v-if="snap?.session?.active" class="text-amber-300 ml-2">已锁定</span>
        </span>
      </div>

      <div v-show="sectionOpen.cfg" class="space-y-6">

      <div class="card cfg p-5" v-if="snap && strategies.length">
        <div class="flex items-center justify-between flex-wrap gap-2">
          <h3 class="text-purple-300">
            声音策略
            <span v-if="rotationActive" class="text-xs font-normal dim">
              · 单夜轮播 (每 {{ fmt(strategyRotationMin, 0) }} 分钟换一种)</span>
            <span v-else-if="strategyPool.length === 1" class="text-xs font-normal dim">
              · 整夜固定一种</span>
            <span v-else class="text-xs font-normal dim">
              · 每次触发随机抽</span>
          </h3>
          <div class="text-xs dim">当前: <span class="kbd">{{ strategyPoolLabel }}</span></div>
        </div>

        <!-- Live rotation progress (only when rotation enabled & active) -->
        <div v-if="rotationActive && rotationState"
             class="mt-3 rounded-lg border border-sky-500/30 bg-sky-500/10 p-3 text-sm">
          <div class="flex items-center justify-between flex-wrap gap-2">
            <div>
              <span class="dim">当前正播</span>
              <span class="kbd ml-1">{{ rotationState.current }}</span>
              <span class="dim ml-2">→ 下一个</span>
              <span class="kbd ml-1">{{ rotationState.next }}</span>
            </div>
            <div class="font-mono text-xs dim">
              本段已 {{ secToClock(rotationState.time_in_block_s) }}
              · 还剩 {{ secToClock(rotationState.time_until_next_s) }} 切换
            </div>
          </div>
          <div class="progress trigger mt-2"><div
            :style="{ width: ((rotationState.time_in_block_s /
                                 (rotationState.rotation_s || 1)) * 100).toFixed(1) + '%' }"></div></div>
          <div class="text-xs dim mt-1">
            会话已运行 {{ secToClock(rotationState.session_elapsed_s) }}
            · 完整一轮 {{ secToClock(rotationState.rotation_s * rotationState.pool.length) }}
          </div>
        </div>

        <div class="mt-4 grid grid-cols-1 md:grid-cols-2 gap-2">
          <button v-for="s in strategies" :key="'pool-'+s.key"
                  class="strat-btn text-left"
                  :class="{ active: strategyPool.includes(s.key) }"
                  :disabled="snap?.session?.active"
                  @click="togglePoolStrategy(s.key)">
            <div class="flex items-center gap-2">
              <span class="font-mono">
                {{ strategyPool.includes(s.key) ? '☑' : '☐' }}</span>
              <span class="font-semibold">{{ s.key }}</span>
              <span class="text-xs dim">· {{ s.name }}</span>
              <span v-if="rotationState && rotationState.current === s.key"
                    class="ml-auto text-xs text-emerald-300">▶ 现在</span>
            </div>
            <div class="text-xs dim mt-0.5 ml-6">{{ s.description }}</div>
          </button>
        </div>

        <div class="mt-4 flex items-center gap-3 flex-wrap">
          <div class="text-xs dim">轮播间隔 (分钟, 0 = 不轮播随机抽):</div>
          <select :value="strategyRotationMin"
                  :disabled="snap?.session?.active"
                  @change="setRotationMinutes(parseFloat($event.target.value))"
                  class="text-sm">
            <option :value="0">0 · 不轮播 (随机)</option>
            <option :value="15">15 分钟</option>
            <option :value="30">30 分钟</option>
            <option :value="45">45 分钟</option>
            <option :value="60">60 分钟</option>
            <option :value="90">90 分钟</option>
            <option :value="120">120 分钟</option>
          </select>
          <div v-if="rotationActive && strategyPool.length >= 2"
               class="text-xs dim">
            一轮总长 ≈
            <span class="kbd">{{
              fmt(strategyRotationMin * strategyPool.length, 0) }} 分钟</span>
          </div>
          <div v-else-if="strategyRotationMin > 0 && strategyPool.length < 2"
               class="text-xs text-amber-300">
            轮播需要至少 2 个策略才会启用
          </div>
        </div>
      </div>

      <div class="card cfg p-5" v-if="snap">
        <h3 class="text-purple-300">自动干预设置</h3>
        <p class="text-xs dim mt-1">
          根据姿态自动播放干预声音。
        </p>

        <div class="mt-4 grid md:grid-cols-3 gap-3">
          <label class="p-3 rounded-lg border border-slate-700/50 bg-slate-900/30">
            <div class="text-xs dim mb-2">自动干预</div>
            <div class="flex items-center gap-2">
              <input type="checkbox" :checked="snap.controller.config.enabled"
                     @change="applyCtrlCfg({enabled: $event.target.checked})">
              <span class="text-sm">
                {{ snap.controller.config.enabled ? '已开启' : '已关闭' }}
              </span>
            </div>
          </label>

          <div class="p-3 rounded-lg border border-slate-700/50 bg-slate-900/30 md:col-span-2">
            <div class="text-xs dim mb-2">灵敏度</div>
            <div class="grid sm:grid-cols-3 gap-2">
              <button class="strat-btn text-left"
                      @click="applyCtrlCfg({trigger_hold_s: 30, response_window_s: 20, cooldown_s: 240, cooldown_no_response_s: 30, retry_trigger_hold_s: 15})">
                <div class="font-semibold">保守</div>
                <div class="text-xs dim">少打扰, 适合正式睡眠</div>
              </button>
              <button class="strat-btn text-left"
                      @click="applyCtrlCfg({trigger_hold_s: 15, response_window_s: 15, cooldown_s: 180, cooldown_no_response_s: 8, retry_trigger_hold_s: 5})">
                <div class="font-semibold">标准</div>
                <div class="text-xs dim">默认实验设置</div>
              </button>
              <button class="strat-btn text-left"
                      @click="applyCtrlCfg({trigger_hold_s: 8, response_window_s: 10, cooldown_s: 90, cooldown_no_response_s: 5, retry_trigger_hold_s: 2})">
                <div class="font-semibold">积极</div>
                <div class="text-xs dim">更快重试, 更容易吵醒</div>
              </button>
            </div>
          </div>

          <label class="md:col-span-3 p-3 rounded-lg border border-slate-700/50 bg-slate-900/30">
            <div class="flex items-center justify-between text-sm">
              <span>声音大小</span>
              <span class="font-mono">{{ fmt(snap.controller.config.level_db, 0) }} dB</span>
            </div>
            <input type="range" min="-40" max="-3" step="1"
                   :value="snap.controller.config.level_db"
                   @change="applyCtrlCfg({level_db: parseFloat($event.target.value)})"
                   class="w-full mt-2">
            <div class="text-xs dim mt-1">越接近 0 越响; 先从 -15 dB 左右开始。</div>
          </label>
        </div>

        <button class="mt-4 text-xs text-purple-300/80 flex items-center gap-1.5"
                @click="showCtrlCfg=!showCtrlCfg">
          <span>{{ showCtrlCfg ? '▾' : '▸' }}</span>
          高级参数 (一般不用动)
        </button>
        <div v-if="showCtrlCfg && snap" class="mt-4 grid sm:grid-cols-2 gap-4">
          <div class="sm:col-span-2 overflow-x-auto rounded-lg border border-slate-700/40">
            <table class="vt">
              <thead>
                <tr><th>步骤</th><th>用到的参数</th><th>含义</th></tr>
              </thead>
              <tbody>
                <tr>
                  <td>1. 识别姿态</td>
                  <td>姿态切换防抖</td>
                  <td>姿态必须稳定一小段时间才算切换，避免翻身抖动造成误判。</td>
                </tr>
                <tr>
                  <td>2. 等待触发</td>
                  <td>仰卧多久才触发</td>
                  <td>持续仰卧达到这个时长后，才准备播放声音。</td>
                </tr>
                <tr v-if="!isSimpleMode">
                  <td>3. 确认鼾声</td>
                  <td>最近有过打鼾窗口 / 额外确认鼾声次数</td>
                  <td>A 模式才使用；S 模式不看鼾声。</td>
                </tr>
                <tr>
                  <td>4. 播放后观察</td>
                  <td>播完观察多久看翻身</td>
                  <td>声音播放后，在这个时间内判断姿态是否离开仰卧。</td>
                </tr>
                <tr>
                  <td>5. 进入冷却</td>
                  <td>翻身成功后冷却 / 无反应后重试冷却</td>
                  <td>成功翻身就休息更久；没反应就短暂停顿后重试。</td>
                </tr>
                <tr>
                  <td>6. 连续重试</td>
                  <td>连发重试蓄力时长</td>
                  <td>上一次没反应时，下一次触发可以等得更短。</td>
                </tr>
              </tbody>
            </table>
          </div>
          <label class="space-y-1">
            <div class="text-xs dim">仰卧多久才触发</div>
            <input type="range" min="2" max="60" step="0.5"
                   :value="snap.controller.config.trigger_hold_s"
                   @change="applyCtrlCfg({trigger_hold_s: parseFloat($event.target.value)})"
                   class="w-full">
            <div class="text-xs dim">{{ fmt(snap.controller.config.trigger_hold_s, 1) }} s</div>
          </label>
          <label class="space-y-1">
            <div class="text-xs dim">播完观察多久看翻身</div>
            <input type="range" min="1" max="30" step="1"
                   :value="snap.controller.config.response_window_s"
                   @change="applyCtrlCfg({response_window_s: parseFloat($event.target.value)})"
                   class="w-full">
            <div class="text-xs dim">{{ fmt(snap.controller.config.response_window_s, 1) }} s</div>
          </label>
          <label class="space-y-1">
            <div class="text-xs dim">翻身成功后冷却</div>
            <input type="range" min="5" max="300" step="5"
                   :value="snap.controller.config.cooldown_s"
                   @change="applyCtrlCfg({cooldown_s: parseFloat($event.target.value)})"
                   class="w-full">
            <div class="text-xs dim">{{ fmt(snap.controller.config.cooldown_s, 0) }} s</div>
          </label>
          <label class="space-y-1">
            <div class="text-xs dim">无反应后重试冷却</div>
            <input type="range" min="0.5" max="60" step="0.5"
                   :value="snap.controller.config.cooldown_no_response_s"
                   @change="applyCtrlCfg({cooldown_no_response_s: parseFloat($event.target.value)})"
                   class="w-full">
            <div class="text-xs dim">{{ fmt(snap.controller.config.cooldown_no_response_s, 1) }} s</div>
          </label>
          <label class="space-y-1">
            <div class="text-xs dim">连发重试蓄力时长</div>
            <input type="range" min="0.5" max="30" step="0.5"
                   :value="snap.controller.config.retry_trigger_hold_s || 2"
                   @change="applyCtrlCfg({retry_trigger_hold_s: parseFloat($event.target.value)})"
                   class="w-full">
            <div class="text-xs dim">
              {{ fmt(snap.controller.config.retry_trigger_hold_s, 1) }} s
              <span v-if="snap.controller.retry_mode" class="text-amber-300">· 当前生效</span>
            </div>
          </label>
          <label class="space-y-1">
            <div class="text-xs dim">姿态切换防抖</div>
            <input type="range" min="0.5" max="10" step="0.5"
                   :value="snap.controller.config.debounce_s"
                   @change="applyCtrlCfg({debounce_s: parseFloat($event.target.value)})"
                   class="w-full">
            <div class="text-xs dim">{{ fmt(snap.controller.config.debounce_s, 1) }} s</div>
          </label>
          <label v-if="!isSimpleMode" class="space-y-1">
            <div class="text-xs dim">需要同时检测到打鼾</div>
            <input type="checkbox" :checked="snap.controller.config.require_snoring"
                   @change="applyCtrlCfg({require_snoring: $event.target.checked})">
          </label>
          <label v-if="!isSimpleMode" class="space-y-1">
            <div class="text-xs dim">“最近有过打鼾”窗口</div>
            <input type="range" min="3" max="60" step="1"
                   :value="snap.controller.config.snoring_recent_s"
                   @change="applyCtrlCfg({snoring_recent_s: parseFloat($event.target.value)})"
                   class="w-full">
            <div class="text-xs dim">
              {{ fmt(snap.controller.config.snoring_recent_s, 0) }} s
              <span v-if="snap.controller.snoring_age_s != null">
                · 距上次鼾声 {{ fmt(snap.controller.snoring_age_s, 1) }} s</span>
            </div>
          </label>
          <label v-if="!isSimpleMode" class="space-y-1 sm:col-span-2">
            <div class="text-xs dim">额外确认鼾声次数</div>
            <input type="range" min="0" max="3" step="1"
                   :value="snap.controller.config.confirm_snore_bouts ?? 1"
                   @change="applyCtrlCfg({confirm_snore_bouts: parseInt($event.target.value)})"
                   class="w-full">
            <div class="text-xs dim">
              {{ snap.controller.config.confirm_snore_bouts ?? 1 }} 次
              <span v-if="snap.controller.state === 'armed'" class="text-amber-300">
                · 已确认 {{ snap.controller.snore_bouts_since_armed ?? 0 }} 次
              </span>
            </div>
          </label>
          <div class="sm:col-span-2 pt-2 border-t border-slate-700/40">
            <div class="text-xs dim mb-2">活跃时段 (空白 = 全夜启用)</div>
            <div class="flex items-center gap-2 flex-wrap">
              <input type="time" :value="snap.controller.config.active_window_start"
                     @change="applyCtrlCfg({active_window_start: $event.target.value})">
              <span class="dim">→</span>
              <input type="time" :value="snap.controller.config.active_window_end"
                     @change="applyCtrlCfg({active_window_end: $event.target.value})">
              <button class="btn ghost text-xs"
                      @click="applyCtrlCfg({active_window_start:'', active_window_end:''})">
                清除
              </button>
            </div>
          </div>
        </div>
      </div>

      <div class="card cfg p-5" v-if="!isSimpleMode">
        <button class="text-purple-300 font-semibold flex items-center gap-2"
                @click="showSnoreCfg=!showSnoreCfg">
          <span>{{ showSnoreCfg ? '▾' : '▸' }}</span>
          鼾声检测阈值  (YAMNet)
        </button>
        <div v-if="showSnoreCfg && snap" class="mt-4 grid md:grid-cols-2 gap-4">
          <label class="space-y-1">
            <div class="text-xs dim">Snoring 概率阈值 (0–1)</div>
            <input type="range" min="0.05" max="0.9" step="0.05"
                   :value="yamnetThresh"
                   @input="yamnetThresh = parseFloat($event.target.value)"
                   @change="applySnoreCfg({snore_prob_thresh: parseFloat($event.target.value)})"
                   class="w-full">
            <div class="text-xs dim">阈值 {{ fmt(yamnetThresh, 2) }} ·
              实测 Snoring {{ fmt(snap.snore.snoring_prob, 2) }}</div>
          </label>
          <div class="space-y-1">
            <div class="text-xs dim">YAMNet 最高类别 (每 0.5 s 刷新)</div>
            <div class="text-sm font-mono">{{ snap.snore.top_class || '—' }}
              <span class="dim">(p={{ fmt(snap.snore.top_prob, 2) }})</span></div>
            <div class="text-xs dim">
              Breathing {{ fmt(snap.snore.breathing_prob, 2) }} ·
              Speech {{ fmt(snap.snore.speech_prob, 2) }} ·
              能量 {{ fmt(snap.snore.energy_db, 1) }} dB</div>
          </div>
          <p class="md:col-span-2 text-xs dim">
            YAMNet 基于 Google AudioSet 预训练, 521 类里 38 类是打鼾;
            阈值 0.3 是常用起点 — 真实打鼾时 p 通常在 0.5–0.9。
          </p>
        </div>
      </div>

      </div><!-- end cfg section -->

      <!-- ═══ 📋 日志 ═══ -->
      <div class="section-bar log" @click="sectionOpen.log = !sectionOpen.log">
        <span class="chevron" :class="{ open: sectionOpen.log }">▶</span>
        📋 日志
        <span class="count" v-if="snap?.events_tail?.length">
          {{ snap.events_tail.length }} 条事件
        </span>
      </div>

      <div v-show="sectionOpen.log" class="space-y-6">
        <div class="card log p-5">
          <pre v-if="snap && snap.events_tail.length"
               class="timeline">{{ snap.events_tail.slice(-30).join('\\n') }}</pre>
          <div v-else class="text-sm dim">(会话开始后显示)</div>
        </div>
      </div>
    </section>

    <!-- ====== tab: 设备连接 ====== -->
    <section v-show="tab==='devices'" class="space-y-6">
      <div class="card p-5">
        <button class="w-full flex items-center justify-between"
                @click="showChestPanel = !showChestPanel">
          <h3 class="text-sky-300">胸带  HSR 1A2.0
            <span class="text-xs dim font-normal">· 呼吸 / 姿态 / 转发 PC-68B SpO2</span>
          </h3>
          <div class="flex items-center gap-2">
            <span v-if="isConnectedChest" class="badge ok">
              <span class="dot"></span>已连接 · 包 #{{ snap.chestband.pkt }}
            </span>
            <span v-else-if="snap?.chestband?.state === 'connecting'" class="badge warn">
              <span class="dot"></span>连接中…
            </span>
            <span v-else-if="snap?.chestband?.state === 'error'" class="badge err">
              <span class="dot"></span>{{ snap.chestband.err || '错误' }}
            </span>
            <span v-else class="badge"><span class="dot"></span>未连接</span>
            <span class="dim text-sm">{{ showChestPanel ? '▾' : '▸' }}</span>
          </div>
        </button>

        <div class="mt-3 flex gap-2 flex-wrap" v-if="!showChestPanel">
          <button class="btn" v-if="!isConnectedChest" @click="chestScanGo">
            扫描并展开</button>
          <button class="btn danger" v-else @click="chestDisconnectGo">
            断开</button>
        </div>

        <div v-if="showChestPanel" class="mt-4 space-y-3">
          <div class="flex gap-2 items-center flex-wrap">
            <button class="btn" :disabled="chestScan.busy" @click="chestScanGo">
              {{ chestScan.busy ? '扫描中…' : '扫描' }}</button>
            <select v-model="chestScan.addr" class="flex-1 min-w-[220px]">
              <option value="">(未选)</option>
              <option v-for="d in chestScan.devices" :key="d.address"
                      :value="d.address">
                {{ d.rssi }} dBm  {{ d.name }}  [{{ d.address.slice(-8) }}]
              </option>
            </select>
          </div>
          <div class="flex items-center gap-3 text-sm">
            <label class="flex items-center gap-1">
              <input type="checkbox" v-model="chestScan.namedOnly">
              <span>只显示有名字</span></label>
            <label class="flex items-center gap-1">
              <input type="checkbox" v-model="chestScan.chestbandOnly">
              <span>仅胸带 (HSR/1A2/SRG…)</span></label>
          </div>
          <div class="flex gap-2 flex-wrap">
            <button class="btn success" @click="chestConnectGo">连接</button>
            <button class="btn danger" @click="chestDisconnectGo">断开</button>
            <button class="btn" @click="chestResyncRTC"
                    :disabled="!isConnectedChest"
                    title="重发 RTC 同步, 清 device_status bit 6, 是唯一对蜂鸣有效的命令">
              🔕 静音蜂鸣
            </button>
            <button class="btn ghost text-xs" @click="showChestDiag = !showChestDiag">
              {{ showChestDiag ? '收起' : '高级诊断' }}
            </button>
          </div>

          <!-- 高级诊断 (默认折起): device_status 解码 + 解绑外设按钮 -->
          <div v-if="showChestDiag" class="mt-3 space-y-3 text-sm">
            <div v-if="snap?.chestband?.vitals?.device_status"
                 class="p-3 rounded-lg border border-amber-500/30 bg-amber-500/10">
              <div class="flex items-center justify-between flex-wrap gap-2">
                <div>
                  <span class="dim">device_status =</span>
                  <span class="kbd ml-1 font-mono">0x{{
                    snap.chestband.vitals.device_status.raw
                      .toString(16).padStart(2,'0').toUpperCase() }}</span>
                </div>
                <div v-if="!snap.chestband.vitals.device_status.flags.length"
                     class="text-emerald-300">无告警</div>
              </div>
              <div v-if="snap.chestband.vitals.device_status.flags.length"
                   class="mt-2 flex flex-wrap gap-1.5">
                <span v-for="f in snap.chestband.vitals.device_status.flags"
                      :key="f.bit"
                      class="badge"
                      :class="(f.bit === 0 || f.bit === 1) ? 'err' : 'warn'">
                  bit{{ f.bit }} · {{ f.label }}
                </span>
              </div>
            </div>
            <div class="flex gap-2 flex-wrap">
              <button class="btn ghost text-xs" @click="chestUnbindAll"
                      :disabled="!isConnectedChest">解绑全部外设</button>
              <button class="btn ghost text-xs" @click="chestSilenceGo"
                      :disabled="!isConnectedChest">发 0x0B (实测无效)</button>
            </div>
            <p class="text-xs dim">
              重发 RTC 是协议里唯一被实测验证有效的"静音"命令. HSRG 固件不响应
              0x0B 状态控制帧. 解绑外设会清 PC-68B 绑定 — 想恢复请重启胸带.
            </p>
          </div>
          <table class="vt mt-3" v-if="snap">
            <thead><tr>
              <th>SpO2</th><th>脉率</th><th>呼吸率</th>
              <th>姿态</th><th>体温</th><th>电池</th>
            </tr></thead>
            <tbody><tr>
              <td :class="snap.chestband.spo2_stale ? 'dim' : ''">
                {{ snap.chestband.vitals.spo2
                   ? snap.chestband.vitals.spo2 + '%'
                   : (snap.chestband.state === 'connected' ? '失效' : '—') }}
              </td>
              <td :class="snap.chestband.pulse_stale ? 'dim' : ''">
                {{ snap.chestband.vitals.pulse
                   ? snap.chestband.vitals.pulse + ' bpm' : '—' }}
              </td>
              <td>{{ snap.chestband.vitals.resp || '—' }}</td>
              <td>{{ posturePretty }}</td>
              <td>{{ snap.chestband.vitals.temp_c != null
                     ? snap.chestband.vitals.temp_c.toFixed(1) + ' ℃' : '—' }}</td>
              <td>{{ snap.chestband.vitals.batt_mv
                     ? snap.chestband.vitals.batt_mv + ' mV' : '—' }}</td>
            </tr></tbody>
          </table>
          <p class="text-xs dim">
            <b>SpO2 / 脉率来源</b>：手指上夹着的 <b>PC-68B 血氧仪</b>，
            通过胸带 BLE 转发过来。只要 PC-68B 开机就会推送，<u>不用</u>在本程序里单独连它。
          </p>
        </div>
      </div>

      <div class="card p-5">
        <h3 class="text-sky-300">音频通道  ·  鼾声麦克 + 干预音输出</h3>
        <p class="mt-2 text-sm dim">
          推荐: <b>电脑麦克风</b>收鼾声 + <b>耳机</b>播放干预音。
          输入和输出走两条独立设备, 不会出现 HFP/A2DP 切换问题。
          以后接入手机麦, 把"鼾声输入"改选成手机即可 (经 USB / 连续互通)。
        </p>
        <div class="mt-4 grid md:grid-cols-2 gap-4">
          <label class="space-y-1">
            <div class="text-xs dim flex items-center justify-between gap-2">
              <span>鼾声输入 (麦克风)</span>
              <span class="font-mono text-sky-200 truncate max-w-[55%]"
                    :title="inputDeviceLabel">当前: {{ inputDeviceLabel }}</span>
            </div>
            <select v-model="audioDevs.inputSel" @change="applyAudioInput"
                    class="w-full">
              <option value="">OS 默认输入</option>
              <option v-for="d in audioDevs.inputs" :key="'in'+d.index"
                      :value="d.index">
                [{{ d.index }}] {{ d.name }}
                ({{ d.max_input_channels }} ch{{ d.is_default ? ' · 默认' : '' }})
              </option>
            </select>
            <div class="text-xs dim">
              麦克状态
              <span class="kbd">{{ snap?.snore?.status || '-' }}</span>
              <span v-if="snap?.snore?.error" class="text-rose-300">
                · {{ snap.snore.error }}</span>
              <span v-if="snap?.snore?.last_audio_age_s != null"
                    :class="snap.snore.last_audio_age_s < 2
                              ? 'text-emerald-300' : 'text-amber-300'">
                · 上次回调 {{ fmt(snap.snore.last_audio_age_s, 1) }}s 前
              </span>
            </div>
          </label>
          <label class="space-y-1">
            <div class="text-xs dim flex items-center justify-between gap-2">
              <span>干预音输出 (播放)</span>
              <span class="font-mono text-sky-200 truncate max-w-[55%]"
                    :title="outputDeviceLabel">当前: {{ outputDeviceLabel }}</span>
            </div>
            <select v-model="audioDevs.outputSel" @change="applyAudioOutput"
                    class="w-full">
              <option value="">OS 默认输出</option>
              <option v-for="d in audioDevs.outputs" :key="'out'+d.index"
                      :value="d.index">
                [{{ d.index }}] {{ d.name }}
                ({{ d.max_output_channels }} ch{{ d.is_default ? ' · 默认' : '' }})
              </option>
            </select>
          </label>
        </div>
        <div class="mt-4 flex gap-2 flex-wrap">
          <button class="btn" @click="reloadAudioDevices"
                  :disabled="audioDevs.loading">
            {{ audioDevs.loading ? '刷新中…' : '重新枚举设备' }}</button>
          <button class="btn" @click="testTone('left')">测试音 (左)</button>
          <button class="btn" @click="testTone('right')">测试音 (右)</button>
          <button class="btn danger" @click="stopAudio">停止</button>
        </div>
      </div>
    </section>

    <!-- ====== tab: 声音设计 ====== -->
    <section v-show="tab==='design'" class="space-y-6">
      <div class="card p-5">
        <h3 class="text-sky-300">选择声音策略</h3>
        <div class="mt-3 flex gap-2 flex-wrap">
          <button v-for="s in strategies" :key="s.key"
                  class="strat-btn" :class="{active: s.key===strat}"
                  @click="strat=s.key">{{ s.key }} · {{ s.name }}</button>
        </div>
        <p v-if="currentStrategy" class="mt-3 text-sm dim">
          {{ currentStrategy.description }}</p>

        <div v-if="currentStrategy?.has_direction" class="mt-5">
          <div class="text-sm dim mb-2">播放方向</div>
          <div class="flex gap-2">
            <button class="dir-chip" :class="{active: dir==='left'}"
                    @click="dir='left'">左声道</button>
            <button class="dir-chip" :class="{active: dir==='right'}"
                    @click="dir='right'">右声道</button>
          </div>
        </div>

        <div class="mt-5 space-y-5">
          <div v-for="g in groupedParams" :key="g.key">
            <div class="text-sm text-sky-200/80 mb-2">{{ g.label }}</div>
            <div class="grid md:grid-cols-2 gap-4">
              <label v-for="p in g.params" :key="p.key" class="space-y-1">
                <div class="flex justify-between text-xs dim">
                  <span>{{ p.label }}</span>
                  <span>{{ fmt(params[p.key], p.step >= 1 ? 0 : (p.step >= 0.1 ? 1 : 2)) }}<span v-if="p.unit"> {{ p.unit }}</span></span>
                </div>
                <input type="range" :min="p.min" :max="p.max" :step="p.step"
                       v-model.number="params[p.key]"
                       @change="onParamChange" @input="onParamChange"
                       class="w-full">
              </label>
            </div>
          </div>
        </div>
      </div>

      <div class="card p-5">
        <div class="flex items-center justify-between flex-wrap gap-2">
          <h3 class="text-sky-300">波形预览  (蓝=L · 橙=R)</h3>
          <div class="flex gap-2 flex-wrap">
            <button class="btn success" @click="playStrategy">播放 (×3)</button>
            <button class="btn danger" @click="stopAudio">停止</button>
            <button class="btn" @click="refreshPreview">换一组噪声</button>
            <button class="btn" @click="exportOne">导出 WAV</button>
            <button class="btn" @click="batchExport">批量导出</button>
          </div>
        </div>
        <div class="mt-3 h-[230px]"><canvas id="c-preview"></canvas></div>
      </div>

      <div class="card p-5">
        <h3 class="text-sky-300">预设</h3>
        <div class="mt-3 grid md:grid-cols-3 gap-3">
          <input type="text" v-model="presetName" placeholder="预设名称" class="md:col-span-1">
          <input type="text" v-model="presetNote" placeholder="备注 (选填)" class="md:col-span-1">
          <button class="btn" @click="savePreset">保存当前参数</button>
        </div>
        <div class="mt-3 grid md:grid-cols-3 gap-3">
          <select v-model="presetSel" class="md:col-span-1">
            <option value="">(选一个)</option>
            <option v-for="p in presets" :key="p.name" :value="presetDisp(p)">
              {{ presetDisp(p) }}
            </option>
          </select>
          <button class="btn" @click="loadPreset">加载</button>
          <button class="btn danger" @click="delPreset">删除</button>
        </div>
      </div>
    </section>

    <!-- ====== tab: 回放 / 审核 ====== -->
    <section v-show="tab==='replay'" class="space-y-6">
      <div class="card p-5">
        <div class="flex items-center justify-between flex-wrap gap-2">
          <h3 class="text-sky-300">历史会话</h3>
          <div class="flex gap-2">
            <button class="btn" @click="reloadHistoryList">刷新列表</button>
            <button class="btn" @click="openSessionsDir">打开 sessions/</button>
          </div>
        </div>
        <div class="mt-3 space-y-2 text-sm" v-if="(historyList || []).length">
          <div v-for="h in historyList" :key="h.id"
               class="p-3 rounded-xl border cursor-pointer transition"
               :class="replaySelectedId===h.id
                        ? 'border-sky-500 bg-sky-500/10'
                        : 'border-slate-700/40 bg-slate-800/40 hover:bg-slate-700/40'"
               @click="openReplay(h.id)">
            <div class="flex items-center justify-between flex-wrap gap-2">
              <div>
                <span class="font-mono text-slate-200">{{ h.id }}</span>
                <span class="ml-2 dim text-xs">
                  {{ h.duration_s != null ? secToClock(h.duration_s) : '—' }} ·
                  干预 {{ h.interventions ?? '—' }} · 胸带 {{ h.packets ?? '—' }} 包
                  · 被试 {{ h.subject || '—' }}
                </span>
              </div>
              <div class="text-xs dim">
                <span v-if="h.ongoing" class="text-amber-300">进行中?</span>
              </div>
            </div>
            <div v-if="h.note" class="text-xs dim mt-1">{{ h.note }}</div>
          </div>
        </div>
        <div v-else class="text-sm dim mt-3">(点「刷新列表」加载历史会话)</div>
      </div>

      <div v-if="replayDetail" class="card p-5 space-y-4">
        <div class="flex items-center justify-between flex-wrap gap-2">
          <div>
            <h3 class="text-sky-300">会话 {{ replayDetail.id }}</h3>
            <div class="text-xs dim">
              被试 {{ replayDetail.meta?.subject_id || '—' }}
              · 开始 {{ replayDetail.meta?.started_at || '—' }}
              · 干预 {{ replayDetail.events?.length || 0 }} 次
            </div>
          </div>
          <div class="flex gap-2">
            <button class="btn primary" @click="runReplayAnalysis"
                    :disabled="replayLoading">
              {{ replayLoading ? '分析中...' : '运行分析脚本' }}
            </button>
          </div>
        </div>

        <div v-if="replayDetail.report_summary &&
                    replayDetail.report_summary.by_strategy">
          <div class="text-sm text-sky-200/80 mb-2">按策略汇总</div>
          <table class="vt">
            <thead><tr>
              <th>策略</th><th>次数</th><th>成功率</th>
              <th>中位潜伏</th><th>前 30s 鼾%</th><th>后 30s 鼾%</th>
            </tr></thead>
            <tbody>
              <tr v-for="(v, k) in replayDetail.report_summary.by_strategy"
                  :key="k">
                <td>{{ k }}</td>
                <td>{{ v.n }}</td>
                <td>{{ v.success_rate_pct != null ? v.success_rate_pct + '%' : '—' }}</td>
                <td>{{ v.latency_median_s != null ? fmt(v.latency_median_s, 1) + 's' : '—' }}</td>
                <td>{{ v.snore_pct_pre_avg != null ? fmt(v.snore_pct_pre_avg, 1) + '%' : '—' }}</td>
                <td>{{ v.snore_pct_post_avg != null ? fmt(v.snore_pct_post_avg, 1) + '%' : '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div>
          <div class="text-sm text-sky-200/80 mb-2">
            所有干预事件 (点击查看 ±60 s 多通道回放)</div>
          <div v-if="!replayDetail.events?.length" class="text-sm dim">
            (这次会话未记录到任何触发事件)
          </div>
          <div v-else class="space-y-1">
            <div v-for="ev in replayDetail.events" :key="ev.base"
                 class="p-2 rounded-lg border cursor-pointer transition"
                 :class="replayActive?.event?.base === ev.base
                          ? 'border-sky-500 bg-sky-500/10'
                          : 'border-slate-700/40 bg-slate-800/40 hover:bg-slate-700/40'"
                 @click="openReplayEvent(ev)">
              <div class="flex flex-wrap items-center gap-3 text-sm">
                <span class="font-mono">{{ ev.time_str }}</span>
                <span>{{ ev.strategy }} / {{ ev.direction }}</span>
                <span :class="ev.success ? 'text-emerald-300' : 'text-rose-300'">
                  {{ ev.success ? '成功' : '未响应' }}</span>
                <span class="dim text-xs">
                  潜伏 {{ ev.latency_s != null ? fmt(ev.latency_s, 1) + 's' : '—' }}
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div v-if="replayActive" class="card p-5 space-y-4">
        <div class="flex items-center justify-between flex-wrap gap-2">
          <div>
            <h3 class="text-sky-300">事件详情 · {{ replayActive.event.time_str }}
              · {{ replayActive.event.strategy }} / {{ replayActive.event.direction }}</h3>
            <div class="text-xs dim">触发时刻为 0 s; 向前看 60 s, 向后看 30 s</div>
          </div>
        </div>

        <div>
          <div class="text-xs dim mb-1">Snoring 概率 (0..1)</div>
          <svg viewBox="0 0 900 120" preserveAspectRatio="none"
               class="w-full h-[120px]" style="background:rgba(15,23,42,0.45);border-radius:8px;">
            <g stroke="rgba(148,163,184,0.15)">
              <line x1="0" x2="900" :y1="60" :y2="60"/>
              <line :x1="600" :x2="600" y1="0" y2="120" stroke="rgba(239,68,68,0.6)" stroke-width="1.5"/>
            </g>
            <polyline :points="replayTraceView(replayActive.traces.snore_prob,
                                {w:900,h:120,ymin:0,ymax:1,tmin:-60,tmax:30})"
                      fill="none" stroke="#38bdf8" stroke-width="1.5"/>
            <text x="8" y="14" font-size="11" fill="#94a3b8">1.0</text>
            <text x="8" y="116" font-size="11" fill="#94a3b8">0.0</text>
          </svg>
        </div>

        <div>
          <div class="text-xs dim mb-1">胸呼吸波形</div>
          <svg viewBox="0 0 900 120" preserveAspectRatio="none"
               class="w-full h-[120px]" style="background:rgba(15,23,42,0.45);border-radius:8px;">
            <line :x1="600" :x2="600" y1="0" y2="120" stroke="rgba(239,68,68,0.6)" stroke-width="1.5"/>
            <polyline :points="replayTraceView(replayActive.traces.chest,
                                {w:900,h:120,
                                 ymin:replayChestYMin, ymax:replayChestYMax,
                                 tmin:-60,tmax:30})"
                      fill="none" stroke="#22c55e" stroke-width="1.3"/>
          </svg>
        </div>

        <div>
          <div class="text-xs dim mb-1">SpO2 (%) · ±60 s 窗口</div>
          <svg viewBox="0 0 900 100" preserveAspectRatio="none"
               class="w-full h-[100px]" style="background:rgba(15,23,42,0.45);border-radius:8px;">
            <line :x1="600" :x2="600" y1="0" y2="100" stroke="rgba(239,68,68,0.6)" stroke-width="1.5"/>
            <polyline :points="replayTraceView(replayActive.traces.spo2,
                                {w:900,h:100,ymin:85,ymax:100,tmin:-60,tmax:30})"
                      fill="none" stroke="#f59e0b" stroke-width="1.5"/>
            <text x="8" y="14" font-size="11" fill="#94a3b8">100</text>
            <text x="8" y="96" font-size="11" fill="#94a3b8">85</text>
          </svg>
        </div>

        <div class="grid md:grid-cols-2 gap-4">
          <div v-if="replayActive.event.files.played">
            <div class="text-xs dim mb-1">实际播放的干预音</div>
            <audio controls preload="metadata" class="w-full"
                   :src="replayActive.event.files.played"></audio>
          </div>
          <div v-if="replayActive.event.files.mic">
            <div class="text-xs dim mb-1">麦克风录音 (±10 s)</div>
            <audio controls preload="metadata" class="w-full"
                   :src="replayActive.event.files.mic"></audio>
          </div>
        </div>
      </div>
    </section>

  </main>

</div>`,
};

App.methods.fmt = fmt;
App.methods.secToClock = secToClock;
App.methods.postureZh = postureZh;

createApp(App).mount('#app');

"""
FastAPI app for the OSA experiment console.

Run with:
  python3 run_designer.py --web
or directly:
  uvicorn server.app:app --host 0.0.0.0 --port 8000

Endpoints:
  GET  /                          — web UI (single page)
  GET  /api/state                 — full snapshot (for initial load)
  WS   /ws                        — periodic state snapshot stream (~4 Hz)
  GET  /api/strategies            — sound strategies metadata
  POST /api/preview               — synthesize waveform for plotting
  POST /api/play                  — play a strategy via local audio sink
  POST /api/stop                  — stop audio
  POST /api/export                — export single WAV
  POST /api/batch-export          — batch export
  GET/POST/DELETE /api/presets    — preset CRUD
  POST /api/ble/chest/scan        — scan chest bands
  POST /api/ble/chest/connect     — connect
  POST /api/ble/chest/disconnect  — disconnect
  GET/POST /api/audio/devices     — list / set audio input/output devices
  POST /api/controller/config     — update thresholds
  POST /api/snore/config          — update YAMNet threshold
  POST /api/session/start         — start recording session
  POST /api/session/stop
  POST /api/trigger               — manual trigger
  GET  /api/history               — session list
  POST /api/history/open          — open sessions/ in Finder
  GET  /api/history/{id}          — session detail (events)
  POST /api/history/{id}/analyze  — run analyze_night.py on this session
  GET  /api/history/{id}/event_detail?base=...   — single event traces
  GET  /api/history/{id}/event/{fname}            — raw event asset
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .runtime import get_runtime


ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / 'web'

app = FastAPI(title='OSA Experiment Console', version='0.2.0')


# ── Static web UI ─────────────────────────────────────────────────────────

@app.get('/')
def index():
    return FileResponse(str(WEB_DIR / 'index.html'))


app.mount('/web', StaticFiles(directory=str(WEB_DIR)), name='web')


# ── Schemas ───────────────────────────────────────────────────────────────

class SessionStartReq(BaseModel):
    tag: str = ''
    subject: str = ''
    note: str = ''


class ConnectReq(BaseModel):
    address: str


class ChestScanReq(BaseModel):
    named_only: bool = True
    chestband_only: bool = False
    timeout: float = 8.0


class PreviewReq(BaseModel):
    strategy: str
    direction: str = 'left'
    params: dict = {}


class PlayReq(BaseModel):
    strategy: str
    direction: str = 'left'
    params: dict = {}
    repeats: int = 1


class ExportReq(BaseModel):
    strategy: str
    direction: str = 'left'
    params: dict = {}


class PresetSaveReq(BaseModel):
    name: str
    strategy: str
    direction: str = 'left'
    params: dict = {}
    note: str = ''
    seed: int = 42


class PresetDelReq(BaseModel):
    name: str


class CtrlCfgReq(BaseModel):
    trigger_hold_s: Optional[float] = None
    retry_trigger_hold_s: Optional[float] = None
    debounce_s: Optional[float] = None
    response_window_s: Optional[float] = None
    cooldown_s: Optional[float] = None
    cooldown_no_response_s: Optional[float] = None
    level_db: Optional[float] = None
    enabled: Optional[bool] = None
    require_snoring: Optional[bool] = None
    snoring_recent_s: Optional[float] = None
    confirm_snore_bouts: Optional[int] = None
    active_window_start: Optional[str] = None
    active_window_end: Optional[str] = None


class SnoreCfgReq(BaseModel):
    snore_prob_thresh: Optional[float] = None


class AudioDevReq(BaseModel):
    set_input: bool = False
    input_device: Optional[int] = None
    set_output: bool = False
    output_device: Optional[int] = None


# ── State / events ────────────────────────────────────────────────────────

@app.get('/api/state')
def api_state():
    return get_runtime().snapshot()


@app.websocket('/ws')
async def ws_stream(ws: WebSocket):
    await ws.accept()
    rt = get_runtime()
    try:
        while True:
            snap = rt.snapshot()
            try:
                await ws.send_text(json.dumps(snap, ensure_ascii=False,
                                              default=float))
            except Exception:
                break
            await asyncio.sleep(0.25)  # 4 Hz
    except WebSocketDisconnect:
        pass


# ── Strategies / sound ────────────────────────────────────────────────────

@app.get('/api/strategies')
def api_strategies():
    return get_runtime().list_strategies()


@app.post('/api/preview')
def api_preview(req: PreviewReq):
    return get_runtime().preview_wave(req.strategy, req.params, req.direction)


@app.post('/api/play')
def api_play(req: PlayReq):
    return get_runtime().play_strategy(req.strategy, req.params,
                                       req.direction, req.repeats)


@app.post('/api/stop')
def api_stop():
    return get_runtime().stop_audio()


@app.post('/api/export')
def api_export(req: ExportReq):
    return get_runtime().export_wav(req.strategy, req.params, req.direction)


@app.post('/api/batch-export')
def api_batch_export():
    return get_runtime().batch_export()


# ── Presets ───────────────────────────────────────────────────────────────

@app.get('/api/presets')
def api_presets_get():
    return get_runtime().list_presets()


@app.post('/api/presets')
def api_presets_save(req: PresetSaveReq):
    return get_runtime().save_preset(req.name, req.strategy, req.direction,
                                     req.params, req.note, req.seed)


@app.delete('/api/presets')
def api_presets_del(req: PresetDelReq):
    return get_runtime().delete_preset(req.name)


# ── BLE chest band ────────────────────────────────────────────────────────

@app.post('/api/ble/chest/scan')
def api_chest_scan(req: ChestScanReq):
    return get_runtime().chest_scan(
        named_only=req.named_only,
        chestband_only=req.chestband_only,
        timeout=req.timeout)


@app.post('/api/ble/chest/connect')
def api_chest_connect(req: ConnectReq):
    return get_runtime().chest_connect(req.address)


@app.post('/api/ble/chest/disconnect')
def api_chest_disconnect():
    return get_runtime().chest_disconnect()


# ── Controller / snore config ─────────────────────────────────────────────

@app.post('/api/controller/config')
def api_ctrl_cfg(req: CtrlCfgReq):
    return get_runtime().set_controller_config(
        {k: v for k, v in req.model_dump().items() if v is not None})


@app.post('/api/snore/config')
def api_snore_cfg(req: SnoreCfgReq):
    return get_runtime().set_snore_config(
        {k: v for k, v in req.model_dump().items() if v is not None})


# ── Audio devices ─────────────────────────────────────────────────────────

@app.get('/api/audio/devices')
def api_audio_devices():
    return get_runtime().list_audio_devices()


@app.post('/api/audio/devices')
def api_audio_devices_set(req: AudioDevReq):
    return get_runtime().set_audio_devices(
        input_device=req.input_device,
        output_device=req.output_device,
        set_input=req.set_input,
        set_output=req.set_output)


# ── Sessions ──────────────────────────────────────────────────────────────

@app.post('/api/session/start')
def api_session_start(req: SessionStartReq):
    return get_runtime().session_start(req.tag, req.subject, req.note)


@app.post('/api/session/stop')
def api_session_stop():
    return get_runtime().session_stop()


@app.post('/api/trigger')
def api_trigger():
    return get_runtime().manual_trigger()


@app.get('/api/history')
def api_history(limit: int = 10):
    return get_runtime().list_sessions(limit=limit)


@app.post('/api/history/open')
def api_history_open():
    return get_runtime().open_sessions_dir()


@app.get('/api/history/{session_id}')
def api_history_detail(session_id: str):
    return get_runtime().session_detail(session_id)


@app.post('/api/history/{session_id}/analyze')
def api_history_analyze(session_id: str):
    return get_runtime().run_session_analysis(session_id)


@app.get('/api/history/{session_id}/event_detail')
def api_history_event_detail(session_id: str, base: str):
    """Load one event's .npz and return the traces as JSON-friendly arrays."""
    import numpy as _np
    from fastapi.responses import JSONResponse
    p = get_runtime().session_event_file(session_id, base + '.npz')
    if p is None:
        raise HTTPException(status_code=404, detail='npz not found')
    try:
        d = _np.load(str(p), allow_pickle=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'load npz: {e}')
    out = {}
    for k in ('trigger_at', 'block', 'strategy', 'direction'):
        if k in d.files:
            v = d[k]
            out[k] = v.item() if hasattr(v, 'item') else v.tolist()
    trigger_at = float(out.get('trigger_at', 0.0))
    def _pair(tk, vk, floatify=True):
        if tk not in d.files or vk not in d.files:
            return []
        ts = d[tk].astype(float) - trigger_at
        ys = d[vk].astype(float if floatify else None)
        return [[round(float(t), 3), float(y)]
                for t, y in zip(ts.tolist(), ys.tolist())]
    out['chest'] = _pair('chest_t', 'chest_y')
    out['spo2'] = _pair('spo2_t', 'spo2_y')
    out['snore_prob'] = _pair('snore_t', 'snore_p')
    if 'snore_t' in d.files and 'snore_flag' in d.files:
        ts = d['snore_t'].astype(float) - trigger_at
        fl = d['snore_flag'].astype(bool)
        out['snore_flag'] = [[round(float(t), 3), bool(f)]
                             for t, f in zip(ts.tolist(), fl.tolist())]
    return JSONResponse(out)


@app.get('/api/history/{session_id}/event/{fname}')
def api_history_event_file(session_id: str, fname: str):
    p = get_runtime().session_event_file(session_id, fname)
    if p is None:
        raise HTTPException(status_code=404, detail='file not found')
    media = 'application/octet-stream'
    if fname.endswith('.wav'):
        media = 'audio/wav'
    elif fname.endswith('.json'):
        media = 'application/json'
    return FileResponse(str(p), media_type=media, filename=fname)

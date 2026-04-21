# 数据格式 · 一晚会话的目录结构

每次会话开始时会在 `sessions/` 下创建目录 `<YYYYMMDD_HHMMSS>_<tag>/`。

```
sessions/20260421_220315_pilot1/
├── meta.json              ← 会话元信息
├── summary.json           ← 结束时写入的汇总
├── events.jsonl           ← 所有事件 (每行一条 JSON)
├── interventions.jsonl    ← 每次干预的精简记录
├── chestband.csv          ← 每秒 vitals 汇总 (SpO2, PR, RR, 姿态…)
├── chestband_0000.npz     ← 胸带原始波形分块 (每块 ~2 s, 压缩存储)
├── chestband_0001.npz
├── ...
├── oximeter.csv           ← PC-68B (如果连了) 每帧输出
├── events/                ← 每次触发的 ±N 秒多通道快照
│   ├── 20260421_223345_A_P2_left.npz         多通道波形快照
│   ├── 20260421_223345_A_P2_left_mic.wav     ±10 s 麦克风录音
│   └── 20260421_223345_A_P2_left_played.wav  当次播出的干预音副本
└── report/                ← scripts/analyze_night.py 产物
    ├── summary.json
    ├── strategy_report.csv
    └── strategy_report.md
```

## meta.json

```json
{
  "session_id": "20260421_220315_pilot1",
  "started_at": "2026-04-21T22:03:15",
  "subject_id": "张三",
  "note": "初次试跑, 阈值默认",
  "protocol": "block_a_pilot",
  "mode": "A",
  "config": {
    "trigger_postures": ["supine"],
    "require_snoring": true,
    "trigger_hold_s": 8.0,
    "snoring_recent_s": 15.0,
    "response_window_s": 10.0,
    "cooldown_s": 180.0,
    "cooldown_no_response_s": 5.0,
    "active_window_start": "01:00",
    "active_window_end": "04:30",
    ...
  }
}
```

## interventions.jsonl

每行一次触发：

```json
{"t": 1713820234.123, "block": "A", "strategy": "P2", "direction": "right",
 "reason": "auto_hold", "posture": "supine", "level_db": -15.0}
```

## events.jsonl

时间线事件流，包括姿态变化、控制器状态跳转、鼾声状态、胸带汇总等。用于
离线重放分析。

## chestband.csv

```
ts,packet_sn,spo2_pct,pulse_rate,resp_rate,heart_rate,gesture,temperature,battery_mv
1713820001.234,1,97,68,14,71,5,36.8,4065
...
```

## chestband_NNNN.npz

胸带原始波形分块压缩存储，每块约 2 秒（由 `SessionRecorder.FLUSH_SECS`
决定）。每个 npz 里有：

- `ts` (N,)：每秒一个包的开始时间戳
- `chest_resp` (N, 25)：胸呼吸（25 Hz）
- `abd_resp` (N, 25)：腹呼吸
- `ecg_ch1..4` (N, 50)：四通道 ECG
- `accel_x, accel_y, accel_z` (N, 25)：三轴加速度
- `spo2_wave` (N, 50)：SpO2 脉搏波

> 为什么分块而不是一个大文件？整夜 ~8 小时 × 25 Hz × 多通道会有几百 MB，
> 分块后单块丢失不影响其他部分，且可流式重放。

## events/*.npz

每次干预触发时（auto 或 manual）自动落盘的 ±N 秒多通道快照。字段：

```python
trigger_at   # unix 时间戳 (s)
block        # 'A' | 'B'
strategy     # 'P1' / 'P2' / ...
direction    # 'left' / 'right' / 'center'

chest_t      # (Nc,) 胸呼吸时间戳
chest_y      # (Nc,) 胸呼吸幅值 (float32)
chest_fs     # float, 25.0

spo2_t       # (Ns,) SpO2 时间戳
spo2_y       # (Ns,) SpO2 % (float32)

snore_t      # (Np,) YAMNet 概率时间戳
snore_p      # (Np,) snoring_prob (0..1)
snore_flag   # (Np,) bool, 是否"正在打鼾"
```

时间覆盖：默认 ±30 s 胸呼吸、±60 s SpO2、±30 s snoring 概率。

## events/*_mic.wav + events/*_played.wav

- `*_mic.wav`：触发前后各 10 秒的麦克风原始录音（16 kHz, mono, float32）
- `*_played.wav`：当次系统真正播出的干预音原样（stereo, DEFAULT_SR）

## report/

`scripts/analyze_night.py` 生成。详见 [`../scripts/analyze_night.py`](../scripts/analyze_night.py)
开头的 docstring。最关键的产物是 **strategy_report.md**：一张按策略分组
的表，对比成功率 / 潜伏 / 前后鼾声占比 / SpO2 改善。

## 隐私与共享

`sessions/` 被 `.gitignore` 全量排除，不会进 git。给研究员共享数据请用
`dist/end_night.command` 生成 zip，通过私有渠道（飞书/网盘/物理介质）传输。

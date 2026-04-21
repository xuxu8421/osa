# 5 种声学策略

定义在 `sounds/strategies.py`。每种策略都注册到 `STRATEGY_REGISTRY`，参数
可在 UI「声音设计」tab 实时拖滑块调，支持批量导出 WAV。

| 代号 | 名字 | 设计意图 | 是否有方向 | 默认时长 |
|------|------|---------|---------|---------|
| **P1** | Spatial Whisper Sweep | 轻柔气流，从一侧扫向另一侧 | 左 / 右 | 0.55 s |
| **P2** | Spatial Double-Pulse Burst | 两个短促气流脉冲，提示感更强 | 左 / 右 | 0.12–0.18 s × 2 |
| **P3** | Unilateral Low-Freq Rumble | 单侧低频闷声，方向感最强 | 左 / 右 | 0.5 s |
| **L1** | Brief Smooth Burst | 最柔和的"提醒一声" | 居中（双声道相同） | 0.18 s |
| **L2** | Brief Rough Burst | 比 L1 颗粒感更强 | 居中 | 0.15 s |

- P 系列 (Posture) 设计用于 **Block A · 体位干预**——用双耳 ITD/ILD 差异
  诱导被试朝反侧翻身。
- L 系列 (Micro-arousal) 设计用于 **Block B · 截断鼾声**——短促居中提示，
  不求翻身，只为打断气道震荡。当前 Block B 尚未启用，L1/L2 仅用于手动测试。

## 合成管线

```
pink_noise(duration) → bandpass(low, high) → envelope(attack, decay)
                   → [optional am(freq, depth)] → level(dB)
                   → spatialize(ITD/ILD, direction) → stereo wave
```

所有步骤在 `sounds/generator.py` 和 `sounds/spatializer.py` 里，可独立调用。

## 每个策略的参数范围

### P1 · Spatial Whisper Sweep

| 参数 | 默认 | 范围 |
|------|------|------|
| duration | 0.55 s | 0.40 – 0.70 |
| band_low | 400 Hz | 200 – 2000 |
| band_high | 2500 Hz | 1000 – 8000 |
| level_db | -15 dB | -40 – 0 |
| ITD | 0.4 ms | 0 – 0.7 |
| ILD | 8 dB | 0 – 15 |

### P2 · Spatial Double-Pulse Burst

| 参数 | 默认 | 范围 |
|------|------|------|
| pulse_dur | 0.15 s | 0.12 – 0.18 |
| gap | 0.32 s | 0.25 – 0.40 |
| band_low | 400 Hz | 200 – 2000 |
| band_high | 2500 Hz | 1000 – 8000 |
| level_db | -15 dB | -40 – 0 |
| ITD | 0.4 ms | 0 – 0.7 |
| ILD | 8 dB | 0 – 15 |

### P3 · Unilateral Low-Freq Rumble

| 参数 | 默认 | 范围 |
|------|------|------|
| duration | 0.5 s | 0.4 – 0.7 |
| band_low | 80 Hz | 40 – 120 |
| band_high | 200 Hz | 120 – 300 |
| level_db | -15 dB | -40 – 0 |
| ITD | 0.6 ms | 0 – 0.7 |
| ILD | 12 dB | 0 – 20 |

### L1 · Brief Smooth Burst

| 参数 | 默认 | 范围 |
|------|------|------|
| duration | 0.18 s | 0.12 – 0.22 |
| band_low | 300 Hz | 200 – 2000 |
| band_high | 3000 Hz | 1000 – 8000 |
| level_db | -15 dB | -40 – 0 |

### L2 · Brief Rough Burst

| 参数 | 默认 | 范围 |
|------|------|------|
| duration | 0.15 s | 0.12 – 0.18 |
| band_low | 300 Hz | 200 – 2000 |
| band_high | 3000 Hz | 1000 – 8000 |
| am_freq | 45 Hz | 30 – 70 |
| am_depth | 0.6 | 0.0 – 1.0 |
| level_db | -15 dB | -40 – 0 |

## 方向选择

`ControllerConfig.direction_policy`：

- `opposite`（默认）：
  - 侧卧 (`left` / `right`) → 播反侧
  - 仰卧 / unknown → 左右随机
- `random`：左右随机
- `left` / `right`：固定一侧（实验对照组用）
- L1 / L2 始终 `center`（无方向）

## 预设

UI 的「声音设计」tab 可以把当前参数存成预设，保存在 `presets/<name>.json`，
下次直接加载。预设跟代码一起入 git，便于复现实验。

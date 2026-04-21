# 实时打鼾检测

系统内置两个后端，接口完全一致（`is_snoring() -> bool`, `metrics() -> dict`,
`start() / stop()`, `set_device()`, `snapshot(seconds) -> ndarray`）。

| 后端 | 类 | 依赖 | 默认 |
|------|----|------|------|
| **YAMNet** | `pipeline/snore_yamnet.py::YamnetSnoreDetector` | `tensorflow-macos`, `tensorflow-hub` | 是 |
| **启发式** | `pipeline/snore.py::MicSnoreDetector` | 仅 numpy | 回退 |

`OsaRuntime.__init__` 先尝试构造 YAMNet；失败（TF 装不上、wheel 不兼容）就自动
退回启发式。UI 上也能手动切。

## YAMNet 后端

- **模型**：Google Research 的 [YAMNet](https://tfhub.dev/google/yamnet/1)。
  输入 16 kHz 单声道、0.96 s 窗，输出 521 类 AudioSet 概率；其中
  **class index 38 = "Snoring"**。
- **推理频率**：`infer_period_s = 0.25 s`，每次拉最近 0.96 s 做一次推理，
  窗口重叠 ~75%。
- **帧内聚合**：YAMNet 在 0.96 s 内部输出 2 个 0.48 s 子帧，我们取
  `scores.max(axis=0)` 再选 "Snoring" 概率——**用 max 而不是 mean**，
  避免单个 0.5 s 的真鼾被均值稀释到阈值以下。
- **hangover**：概率连续 2 秒不超过阈值才撤销 `is_snoring=True`。
- **Apple Silicon GPU**：`tensorflow-metal` 会把模型搬到 Metal 设备，M 系
  上单次推理约 10 ms。
- **首启动**：从 TF Hub 下载约 17 MB 的模型到 `~/.cache/tfhub_modules/`，约 10-20 秒。

### 默认阈值

- `snore_prob_thresh = 0.3`（UI 滑块可 0.05–0.9）
- 打鼾时典型概率 **0.5–0.9**；大声说话 / 咳嗽 < 0.05；深呼吸 0.1–0.3。
- 基于 AudioSet 基准，Snoring 类的 AUC > 0.95。

## 启发式后端（回退）

- 两个特征：
  - **能量**：1.5 s 窗 RMS → dB
  - **低频带能比**：80–500 Hz 占总谱能量的比例
- 两者都超阈值 + hangover 1.2 秒就判为"正在打鼾"。
- 默认阈值：`energy_db > -45`，`band_ratio > 0.55`。
- 对安静环境里距离 <1 m 的打鼾能跑；对实际卧室环境抗噪能力差——正式实验
  请用 YAMNet。

## UI 时间线

「实时实验」tab 上方的**鼾声判决时间线**卡片用 SVG 渲染最近 90 秒：

- 蓝线 = 每 0.25 s 的 Snoring 概率
- 绿色阴影 = 被判为"打鼾中"的时段（含 hangover 保持）
- 橙色虚线 = 当前阈值

阈值滑块会实时改变绿色阴影的出现位置，便于调参。

## 后端切换

```bash
# API
curl -XPOST http://localhost:8000/api/snore/backend \
     -H 'Content-Type: application/json' \
     -d '{"backend": "yamnet"}'   # 或 "heuristic"
```

UI 上在「控制器阈值」面板顶部按钮切换。切换时会保留当前选的麦克风设备，
重启音频流。

## 离线调参

使用 `scripts/tune_snore.py` 对真实 WAV 离线跑：

```bash
# 启发式，网格扫阈值
python3 scripts/tune_snore.py my_snore.wav \
    --sweep-energy=-60,-50,-45,-40,-35 \
    --sweep-br=0.3,0.4,0.5,0.6,0.7

# YAMNet，打印 top-5 类别和逐 0.48 s 概率时间线
python3 scripts/tune_snore.py --yamnet my_snore.wav
```

## 选型依据

早期版本先做了启发式（能在没 TF 的机器上跑），上线后实测对非标准鼾声（比如
打鼻鼾、呼噜声夹断奏）抗噪能力差，因此升级为 YAMNet。启发式保留作为紧急
fallback，同时也作为校准基准：两者同时跑 N 小时，能看出 YAMNet 在哪些场景
纠正了启发式的错判。

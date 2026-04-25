# 实时打鼾检测

接口：`is_snoring() -> bool`, `metrics() -> dict`,
`start() / stop()`, `set_device()`, `snapshot(seconds) -> ndarray`。

后端：`pipeline/snore_yamnet.py::YamnetSnoreDetector`
依赖：`tensorflow-macos`, `tensorflow-hub`

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

## 默认阈值

- `snore_prob_thresh = 0.3`（UI 滑块可 0.05–0.9）
- 打鼾时典型概率 **0.5–0.9**；大声说话 / 咳嗽 < 0.05；深呼吸 0.1–0.3。
- 基于 AudioSet 基准，Snoring 类的 AUC > 0.95。

## UI 时间线

「实时实验」tab 上方的**鼾声判决时间线**卡片用 SVG 渲染最近 90 秒：

- 蓝线 = 每 0.25 s 的 Snoring 概率
- 绿色阴影 = 被判为"打鼾中"的时段（含 hangover 保持）
- 橙色虚线 = 当前阈值

阈值滑块会实时改变绿色阴影的出现位置，便于调参。

## 输入设备

默认走 OS 默认输入（Mac 内置麦克风）。在 Web UI「设备连接」tab 可换成任意
sounddevice 列出的设备：USB 麦、声卡、连续互通麦克风（iPhone）等。
切换设备时 InputStream 会自动重启。

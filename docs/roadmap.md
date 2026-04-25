# OSA 声音干预实验 · 进展与路线图

> 最近更新：2026-04-21 · 当前分支 `main @ cbea283`

## 当前进度（Block A 主体已完成）

| 模块 | 状态 | 说明 |
|------|:----:|------|
| 胸带数据采集 (HSR 1A2) | ✅ | 呼吸波 / 姿态 / ECG / 加速度稳定 |
| SpO2 / 脉率 | ✅ | 经 PC-68B 转发（胸带 HSRG 变体自身无 PPG，已实测验证） |
| 打鼾检测 (YAMNet) | ✅ | Apple Silicon Metal 加速，阈值 0.30，±0.25 s 推理 |
| 闭环控制器 | ✅ | armed → triggered → playing → observe → cooldown |
| 连发节奏优化 | ✅ | 首次 8 s 蓄力，无反应后 `retry_trigger_hold_s=2 s`，间隔 ~8 s |
| 活跃时段门控 | ✅ | 支持 N2 时段 (如 01:00–04:30) |
| Web UI / 回放审核 tab | ✅ | Vital 卡片 + 鼾声 SVG 时间线 + 回放多通道波形 |
| 每次触发 ±N s 快照 | ✅ | 胸呼吸 / SpO2 / 鼾声概率 / 麦克录音 / 干预音 |
| 夜结束自动分析 | ✅ | `scripts/analyze_night.py` → summary + per-strategy 表 |
| 一键部署包 (.command) | ✅ | 非技术被试也能跑 |
| 文档 | ✅ | `docs/` 9 个 md，含协议实测报告 |

## 未完成 / 待研究

- 🟡 **手机麦克风**：当前 pilot 用 Mac 内置麦 + 耳机输出（输入输出走两条独立设备，回避 macOS 蓝牙 HFP 限制）。下一步评估用 Android 手机当无线麦（推流到 Mac），让被试床头摆位更灵活；iPhone 方向暂搁置（连续互通强制同 Apple ID，对被试不友好）。
- 🟡 胸带 HSRG 固件不给 `resp_rate` / `gesture` / 电池字段（都是 0 或跳变），上层已用 `accel` 算姿态 + RIP 峰检测算呼吸率兜底；长期建议换官方固件或基础版胸带。
- ⚪ Block B（微唤醒截断）代码骨架在，未启用。

---

本文件以下部分描述后续三个增量方向，按优先级排序。

## 1 · OSA 事件检测（Phase 2）

**动机**：目前系统用"打鼾"作为 OSA 的代理，但：

- 打鼾 ≠ OSA。相当一部分打鼾者 AHI < 5。
- 临床定义 OSA 事件：**Apnea**（呼吸停止 ≥ 10 s）+ **Hypopnea**（呼吸幅度
  降 ≥ 50% 持续 ≥ 10 s 且 SpO2 下降 ≥ 3%）。
- 真正做"OSA 干预"需要直接检测 Apnea/Hypopnea 事件。

**数据原料已齐**：

| 信号 | 来源 | 采样率 |
|------|------|-------|
| RIP 胸呼吸波 | 胸带 `chest_resp` | 25 Hz |
| RIP 腹呼吸波 | 胸带 `abd_resp` | 25 Hz |
| SpO2 | 胸带 `vitals.spo2_pct` + `spo2_wave` | 1 Hz / 50 Hz 波 |
| IMU | 胸带 `accel_x/y/z` | 25 Hz |

**实现计划**：

- `pipeline/respiratory.py::ApneaDetector`
  - Apnea：10 s 滑窗内 RIP 峰-峰 ≤ 10% 基线（基线=前 2 分钟 p90）
  - Hypopnea：RIP 峰-峰 ≤ 50% 基线 + 后 30 s 内 SpO2 ↓ ≥ 3%
  - 输出：`resp.state ∈ {normal, hypopnea, apnea}` 每秒一帧

- `pipeline/arousal.py::MovementDetector`
  - IMU 三轴加速度向量的高频能量滑窗 > 阈值 → body movement
  - 连续 ≥ 2 s → arousal 事件

- `pipeline/state_classifier.py::OSAStateClassifier`
  - 融合以上 + 姿态 + snoring_prob
  - 每秒输出 4 状态之一：Normal / SustainedSnoring+Supine / SnoreBout / Arousal
  - 先规则版（if-else），后期可替换为 XGBoost / 轻量 CNN

**验证**：用 `scripts/eval_state_classifier.py` 对已有 session 跑分类，在
Web 回放 tab 肉眼抽查，迭代阈值。

**改造控制器**：Block A 触发条件升级为
`(RIP apnea OR RIP hypopnea OR sustained_snoring) AND supine`，
**真正做 OSA 干预**而不是仅鼾声干预。

## 2 · Block B · 微唤醒截断

**目标**：不求翻身，只打断气道震荡。用 L1/L2 短促居中声。

**和 Block A 的区别**：

| 维度 | Block A | Block B |
|------|---------|---------|
| 触发 | 仰卧 + 持续鼾 8 s | 鼾声簇持续 3 s |
| 策略 | P1/P2/P3 方向化 | L1/L2 居中 |
| 响度 | -15 dB | -25 dB (亚觉醒) |
| 成功判据 | 翻身离仰卧 | 鼾停 8 s |
| 冷却(成功) | 180 s | 60 s |
| 冷却(无响应) | 5 s | 5 s |

**实现计划**：`pipeline/controller_b.py::SnoreBurstController`，独立状态机、
与 A 共享 audio sink 和 session。一晚只跑其中一种（会话开始时选 mode=A/B）。

**约束**：等 Block A 在 3 夜以上真实数据上稳定后再启用 B，避免同时改两
处导致实验变量混杂。

## 3 · 个性化策略（远期 · PPO）

实习生提交的 PPO 方案（见 `基于PPO算法的端云协同个性化OSA声学干预系统
实验方案.pdf`）规划了这条路：

- 预训练：5-10 名 PSG 确诊 OSA 患者的 (s_t, a_t, r_t, s_{t+1}) 数据 → 行为克隆
  + 世界模型 LSTM + 离线 PPO
- 在线微调：每晚轨迹上传云端，个性化权重下发

**前置条件**：

- 需要 **1** 的状态检测可用（Reward 无法在没有状态标签时计算）
- 需要 **2** 的基础策略对比数据（Block A / Block B 分策略成功率）
- 需要临床合作（PSG 金标准标注）——目前不具备
- 需要边缘设备（实际耳机 SDK）——目前用 macOS + AirPods 简化

**结论**：PPO 是 2027 年产品化目标，目前 (2026-04) 应把精力放在**状态检测**
和**基础策略对比**上。

## 4 · 其他工程改进（零散）

- [ ] **荣耀耳机实测**：验证是否能做"单耳机同时收放音"，若可行则打开产品形态。
- [ ] 实习生接手 1 · 写 `ApneaDetector` 和 `MovementDetector`
- [ ] 把 ui/designer.py 正式标记 deprecated 并从 README 移除
- [ ] SessionRecorder 增加阶段性 summary（每 30 min 写一次 meta，防 crash）
- [ ] 支持多设备同步（多个胸带、多个被试——如果以后做对照实验）
- [ ] 为非 macOS 目标编写 sounddevice 备份驱动（Linux pilot）

## 近期里程碑（可作为汇报节奏）

| 周 | 目标 |
|----|------|
| W1 (本周) | 荣耀耳机实测 + 2 位被试 pilot 夜测；分析报告与 AirPods 对比 |
| W2 | 根据 pilot 数据调参（`retry_trigger_hold_s` / `response_window_s` / 活跃时段） |
| W3-4 | 实习生落地 `ApneaDetector` + `MovementDetector`（路线图 §1） |
| W5 | Block A 触发条件升级为"Apnea/Hypopnea + 仰卧"，真 OSA 干预 |
| W6+ | Block B 开启，两种策略 A/B 对照 |

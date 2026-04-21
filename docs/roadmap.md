# 远期路线图

当前代码完整实现了 **Block A · 体位干预**（仰卧 + 打鼾触发 → 定向声音诱导翻身）。
本文件描述后续三个增量方向，按优先级排序。

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

- [ ] 实习生接手 1 · 写 `ApneaDetector` 和 `MovementDetector`
- [ ] 把 PC-68B 驱动封装成插件模式，允许运行时禁用
- [ ] 把 ui/designer.py 正式标记 deprecated 并从 README 移除
- [ ] SessionRecorder 增加阶段性 summary（每 30 min 写一次 meta，防 crash）
- [ ] 支持多设备同步（多个胸带、多个被试——如果以后做对照实验）
- [ ] 为非 macOS 目标编写 sounddevice 备份驱动（Linux pilot）

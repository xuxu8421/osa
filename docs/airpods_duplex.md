# 单耳机收放音方案分析

**背景**：Block A 需要空间化立体声（ITD/ILD），同时系统又要实时采集麦克风
判断鼾声。一副 AirPods 能否既出立体声又录音？

## 根本限制：AirPods 的两种 profile

| Profile | 输出 | 输入 |
|---------|------|------|
| **A2DP** (Advanced Audio Distribution) | 44.1/48 kHz **立体声** | ❌ 不提供 |
| **HFP/SCO** (免提协议) | 8/16 kHz **单声道** | 16 kHz 单声道 |

**这两个 profile 互斥**。macOS / iOS 根据"是否有应用打开 AirPods 麦克风"自动
切换：一旦有进程开了输入流，整副耳机被拉进 HFP，输出也随之降为单声道。

## 三种方案对比

### 方案 A · 默认方案：Mac 内置麦 + AirPods 只做输出

**当前系统默认走这条路。**

- 输入：MacBook 的 3 颗波束麦克风，距离嘴 30–40 cm
- 输出：AirPods 永远保持 A2DP 立体声
- 优点：
  - 空间化保真
  - 麦克风质量高（波束降噪）
  - 无切换延迟、无系统提示音
  - 实现简单
- 缺点：需要 MacBook 放在被试附近

### 方案 B · 单耳机模式（本仓库实验性实现）

开关位置：Web UI "设备连接" tab → "音频通道" 卡片底部 →
**单耳机模式 (实验性)** 勾选框。

**工作原理**：

1. 平时 AirPods 处于 HFP（被我们打开的麦克风锁在 HFP 模式）
2. 要播放时：
   a. `LocalAudioSink` 的 `before_play` hook 调 `snore.stop()` 关掉输入流
   b. 等 `preroll_s`（默认 1.2 s）让 macOS 把 AirPods 切到 A2DP
   c. `sd.play` 输出立体声
   d. `after_play` hook 等播放结束 + `postroll_s`（默认 0.3 s）
   e. 调 `snore.start()` 重开输入流（macOS 又把 AirPods 拉回 HFP）
3. 期间麦克风有约 **`preroll + 播放时长 + postroll`** ≈ 5 秒空档

**代码位置**：
- `pipeline/audio.py::LocalAudioSink.play` 的 `before_play/after_play` hook
- `server/runtime.py::OsaRuntime._audio_before_play/_audio_after_play`

**已知问题**：
- macOS 切换 profile 时会有系统提示音（音量渐变或轻微 pop）；具体能否接受
  要实测——这就是为什么我们做成"实验性"开关让你自己听一下。
- HFP 麦克风音质比 MacBook 波束麦差，YAMNet 概率会降低（Snoring 类的
  band 在 80–500 Hz，HFP 的 16 kHz 单声道勉强能覆盖）。
- AirPods 连接不稳定时偶尔卡在 HFP 不切回来；目前靠 `sd._terminate()
  + sd._initialize()` 兜底。

**启停**：

```bash
curl -XPOST http://localhost:8000/api/audio/single-earbud \
     -H 'Content-Type: application/json' \
     -d '{"enabled": true, "preroll_s": 1.2, "postroll_s": 0.3}'
```

### 方案 C · 两副耳机

用两副 AirPods / 或 AirPods + 有线耳机。一副专门做输入（HFP），一副专门做
输出（A2DP）。这是蓝牙协议唯一可以真·全双工立体声的路子，但现实里受试者不
会愿意戴两副耳机。**不推荐**，除非是实验室内严格对照组。

## 推荐

| 场景 | 推荐方案 |
|------|---------|
| 目前 pilot（最多 2 个被试，MacBook 放枕边） | **A**（默认，什么都不用改） |
| 验证"未来只用一副耳机"的产品形态可行性 | B（单耳机模式），侧重听切换提示音大小是否可接受 |
| 严谨实验室对照 | C |

B 方案保留在仓库里作为未来切换到"产品形态"的桥梁；A 当作主力。两者共享所有
上层闭环逻辑，切换只是换一个 audio I/O 模式。

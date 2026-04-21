# 单耳机收放音方案分析

**背景**：Block A 需要空间化立体声（ITD/ILD），同时系统又要实时采集麦克风
判断鼾声。一副 AirPods 能否既出立体声又录音？

> **TL;DR（2026-04-21 实测更新）**：**不行**。虽然代码里保留了"单耳机模式"
> 作为实验性开关，但 macOS CoreAudio 的状态机和 PortAudio 的底层
> 路径注定它会把 AirPods 粘在 HFP 单声道，**强烈建议永远走方案 A**
> （MacBook 麦 + AirPods 立体声输出，不勾单耳机模式）。

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

### ⚠️ 2026-04-21 实测报告：不推荐使用

实际走通之后发现**还有更深的问题**：即使 preroll 拉到 **4.7 s**（滑块最大值附近），
AirPods Pro 也**不会**切回 A2DP，输出始终是单声道。原因分析：

**FaceTime / 微信通话**类官方 App 之所以能 HFP↔A2DP 秒切，是因为它们用了
Apple 的 **`AVAudioSession`** API：
- 通话开始 → `AVAudioSession.setCategory(.playAndRecord)` → CoreAudio **显式收到**"要录音"
- 通话结束 → `AVAudioSession.setActive(false)` → CoreAudio **显式收到**"释放会话"
- CoreAudio 内部有引用计数：没人声明需要 HFP → 立刻切回 A2DP。

**我们这条 `sounddevice → PortAudio → AudioUnit` 路径**：
- `sd.InputStream(device=AirPods)` 只是"借一个 AudioUnit"，**没有走 AVAudioSession**。
- `stream.close()` 只释放 AudioUnit handle，**没有给 CoreAudio 明确的 "session 结束" 信号**。
- 再加上我们的 FastAPI 服务进程**长期运行**，CoreAudio 会把"这个进程以前用过 HFP"
  记下来，倾向于继续保持 HFP（假设我们马上还要用）。
- 结果：HFP 粘死，**只有断开蓝牙重连 / 把 AirPods 取下重戴** 才能回到 A2DP。

**对比验证**：同一台 Mac，同一副 AirPods，播放 YouTube 视频时立体声完美，通完话后
秒切回立体声——但一进本程序勾单耳机模式，立体声就没了，且要蓝牙重启才恢复。

**为什么不直接改用 AVAudioSession？**
- Python 原生无绑定，需要 PyObjC。
- 需要和 PortAudio 的状态同步，两个音频栈各管各的，容易互相打架。
- **即使做完，也绕不过物理限制**：AirPods 同一时刻只能二选一，没有第三种 profile。

**所以定性**：本项目的 Block A **必须立体声**（方向差是干预的核心机制），
**输入必然不能是 AirPods**。把单耳机模式当作"副作用较大的研究性遗留实验开关"
对待即可，**实验正式运行时务必关闭、输入选 MacBook 内置麦**。

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
| 验证"未来只用一副耳机"的产品形态可行性 | B（单耳机模式）× **实测不可用**，且会把 AirPods 卡在 HFP |
| 严谨实验室对照 | C |

B 方案保留在仓库里作为未来切换到"产品形态"的桥梁；**但就 2026-04 当前的
sounddevice/PortAudio + macOS 组合而言，不可用**。A 当作主力，两者共享所有
上层闭环逻辑，切换只是换一个 audio I/O 模式。

## 如果 AirPods 卡在 HFP 了

在使用过程中如果不小心勾了单耳机模式、或者手动把输入切过 AirPods，可能会把
耳机卡在 HFP 模式（听视频也是单声道、低采样率）。恢复方法：

1. **（最快）** 点 Mac 右上角状态栏蓝牙图标 → 关闭 → 等 3 秒 → 打开。AirPods 自动重连，回到 A2DP。
2. 或：把 AirPods **取出 3 秒再放回去**，让耳机主动重新协商。
3. 回到本程序 → "设备连接" → "重新枚举设备" → 确认"干预音输出"下拉显示
   `(2 ch · 默认)`（通道数是 2 才是 A2DP；若是 1 ch 说明还在 HFP，需要再复位一次）。

恢复后，**不要再勾单耳机模式**，输入保持 `MacBook Air 麦克风`。

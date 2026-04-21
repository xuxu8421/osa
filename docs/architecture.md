# 系统架构

## 一句话

**"硬件 → 感知 → 决策 → 执行 → 落盘"，全部由一个内存级 EventBus 串起来。**

## 数据流

```
 ┌──────────────────────────┐
 │  HSR 1A2.0 胸带 (BLE)     │──chestband.data──┐
 │    └ 姿态/呼吸/SpO2/ECG   │                  │
 └──────────────────────────┘                  │
 ┌──────────────────────────┐                  │
 │  PC-68B 血氧仪 (BLE, 可选) │──oximeter.reading┤
 └──────────────────────────┘                  │         ┌───────────────┐
                                               ├────────▶│  EventBus     │
 ┌──────────────────────────┐                  │         │ (发布/订阅)    │
 │  Mac 麦克风 (sounddevice) │──snore.state─────┤         └───────┬───────┘
 │    └ YAMNet 或启发式       │                  │                 │
 └──────────────────────────┘                  │                 │
                                               │                 ├─▶ PostureAnalyzer
                                               │                 │     └ posture.sample/change
                                               │                 │
                                               │                 ├─▶ ClosedLoopController
                                               │                 │     └ intervention.triggered
                                               │                 │       intervention.state
                                               │                 │       intervention.response
                                               │                 │       intervention.error
                                               │                 │
                                               │                 └─▶ SessionRecorder
                                               │                       └ 所有事件 → jsonl/csv/npz
                                               │
                                               │         ┌───────────────┐
                                               └────────▶│ OsaRuntime    │ (单例)
                                                         │   + snapshot()│─── WebSocket /ws ──▶ 前端
                                                         └───────────────┘
                                                         ┌───────────────┐
                                                         │ FastAPI (uvicorn) │
                                                         │   REST + /ws       │
                                                         └───────────────────┘
```

## 核心类

| 类 | 文件 | 职责 |
|----|------|------|
| `EventBus` | `pipeline/events.py` | 线程安全的发布-订阅，所有跨模块沟通都走这里 |
| `PostureAnalyzer` | `pipeline/posture.py` | 从胸带 IMU 出姿态分类 + debounce |
| `MicSnoreDetector` | `pipeline/snore.py` | 启发式（能量 + 80–500 Hz 带能比）；YAMNet 装不上时的后备 |
| `YamnetSnoreDetector` | `pipeline/snore_yamnet.py` | 主力；TF Hub `yamnet/1`，每 0.25 s 推理一次 |
| `ClosedLoopController` | `pipeline/controller.py` | Block A 状态机；触发 / 播放 / 观察 / 冷却 |
| `LocalAudioSink` | `pipeline/audio.py` | sounddevice 播放 + 单耳机模式 hook |
| `SessionRecorder` | `pipeline/recorder.py` | 订阅所有事件 → `sessions/<id>/` 落盘 |
| `OsaRuntime` | `server/runtime.py` | 上面这一锅的 facade，一个进程一个实例 |
| FastAPI app | `server/app.py` | REST + WebSocket，纯 IO 层 |
| SPA | `web/app.js` | Vue 单文件应用，靠 `/ws` 拿 ~4 Hz 快照渲染 |

## 线程模型

| 线程 | 起源 | 做什么 |
|------|-----|--------|
| uvicorn main | 进程启动 | 跑 FastAPI、HTTP 路由、WebSocket handler |
| BLE asyncio loop | `OsaRuntime._start_ble_loop` | 独占一条 event loop，胸带/血氧的 Bleak 都在这跑 |
| sounddevice audio callback | PortAudio | 麦克风 16 kHz 回调；只往 ring buffer 写 |
| YAMNet worker | `YamnetSnoreDetector._worker_loop` | 每 0.25 s 拉最近 0.96 s 做推理 |
| SessionRecorder flusher | `SessionRecorder._flush_loop` | 每 2 s 把波形 buffer 写一个压缩 `.npz` 块 |
| Controller auxiliary | `threading.Thread` ad-hoc | 播放 + 观察 + 冷却倒计时（释放回 idle） |

**线程安全规则**：EventBus handler 随时可能在上面任意线程触发，因此所有
**可变状态**都用 `threading.Lock` 或只做 append 操作；读侧（`snapshot()`）只
读 immutable 属性和 deque 头部。

## 事件清单

| 事件 | 发者 | 常见订阅者 |
|------|------|-----------|
| `chestband.data` | `devices/chestband.py` | Recorder, PostureAnalyzer, Runtime(vitals) |
| `oximeter.reading` | `devices/oximeter.py` | Recorder, Runtime(vitals) |
| `posture.sample` | PostureAnalyzer | Controller, Runtime |
| `posture.change` | PostureAnalyzer | Controller, Recorder |
| `snore.state` | `MicSnoreDetector` / `YamnetSnoreDetector` | Runtime(history), Recorder |
| `intervention.state` | Controller | Recorder, Runtime |
| `intervention.triggered` | Controller | Recorder, Runtime (→ snapshot) |
| `intervention.response` | Controller | Recorder, Runtime |
| `intervention.error` | Controller | Runtime (banner) |
| `session.marker` | OsaRuntime | Recorder |

## 控制流：一次干预的完整时间线

详见 [`block_a_pipeline.md`](block_a_pipeline.md)。

## 硬件抽象

`pipeline/audio.py` 有两个实现：

- `LocalAudioSink`：靠 sounddevice / PortAudio 向 OS 输出设备播放。**当前默认**。
- `HeadsetAudioSink`：占位类。将来接入耳机平台 SDK 时，**只实现这个类**，上层
  Controller 代码**零改动**（因为它只依赖 `AudioSink` 抽象基类）。

类似地，`pipeline/sensors.py` 的 `Sensor` 协议是为了把"来自不同厂家的传感器"
统一成同样的事件接口；未来换成自家耳机平台上报的数据，只需实现一个新的
`HeadsetSensor` 即可。

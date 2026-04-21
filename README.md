# OSA 打鼾干预实验工作台

一个可在家中过夜运行的 OSA（阻塞性睡眠呼吸暂停）**体位干预**实验平台。
把蓝牙胸带、血氧仪、YAMNet 实时打鼾检测、定向空间音效干预器串成闭环，
夜间自动触发、落盘所有原始信号和干预事件、次日一键生成分析报告。

```
                  ┌────────────────┐   BLE (1A2.0)    ┌───────────────┐
                  │  HSR 1A2 胸带   ├─────────────────▶│ 姿态/呼吸/SpO2 │
                  └────────────────┘                  │     解析       │
                  ┌────────────────┐   BLE (可选)      └───────┬───────┘
                  │  PC-68B 血氧仪  ├────────────────▶        (冗余)
                  └────────────────┘                          │
                  ┌────────────────┐   sounddevice            ▼
                  │ Mac 内置麦克风   ├──┐            ┌─────────────────┐
                  └────────────────┘  │            │  EventBus +      │
                                      ├──▶ YAMNet ─▶│ 闭环控制器       │
                                      │   打鼾概率   │  (Block A 状态机)│
                                      │            └────────┬────────┘
                                      │                     │ 触发
                                      │            ┌────────▼─────────┐
                                      │            │  5 种声学策略合成 │
                                      │            └────────┬─────────┘
                                      │                     │
                                      │            ┌────────▼─────────┐
                                      │            │  AirPods / 扬声器 │
                                      │            └──────────────────┘
                                      │
                                      └───▶ SessionRecorder ──▶ sessions/<id>/
```

## 亮点

- **实时打鼾检测**：Google [YAMNet](https://tfhub.dev/google/yamnet/1) 预训练模型
  （AudioSet 521 类里的 **Snoring**），Apple Silicon Metal GPU 加速；
  TensorFlow 未装时自动回退到 RMS + 80–500 Hz 带能比的启发式实现。
- **双声道方向干预**：5 种策略 P1/P2/P3（带 ITD/ILD 空间化，左右耳差异）+
  L1/L2（居中短声，留给后续 Block B 截断鼾声）。
- **完整状态机**：仰卧 ∧ 最近有鼾声 → 计时 8 秒 → 触发 → 播放 → 10 秒观察 →
  成功 180 秒长冷却 / 无反应 5 秒短冷却（全部可调）。
- **活跃时段**：一整夜不可能一直干预，支持指定起止时间（例如 `01:00–04:30`）。
- **每次触发自动落盘**：胸带呼吸波 ±30 s、SpO2 ±60 s、YAMNet 概率 ±30 s、
  麦克风 ±10 s 录音、当次播放的干预音副本，方便事后审核。
- **Web 控制台**：FastAPI + 单页 Vue（无编译），手机同 Wi-Fi 下也能访问。
- **回放/审核 Tab**：所有历史会话一眼浏览，每次干预可以同时看 3 条波形 + 听
  当次干预音 + 听麦克风录音。
- **夜间分析脚本**：`scripts/analyze_night.py` 一行命令出 `strategy_report.csv`
  和 `strategy_report.md`，按策略分组对比成功率 / 潜伏 / 前后鼾声占比 / SpO2 改善。

## 快速启动（开发机）

```bash
# 1. 安装依赖（推荐 venv）
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. 启动 Web 控制台
python3 run_designer.py --web
# 然后浏览器打开  http://localhost:8000
```

> ⚠️ 首次启动会从 TF Hub 下载 YAMNet 模型（约 17 MB），大概 10 秒。
> 没有网络 / 装不上 TensorFlow 时系统会自动回退到启发式鼾声检测，Block A
> 主流程仍可用。

**有被试、要真的跑一晚**：看 [`dist/README.md`](dist/README.md)，里面是给非
技术背景被试的一键脚本包（双击 `.command` 即可）。

## 两种运行模式

| 模式 | 入口 | 用途 |
|------|------|------|
| **Web 控制台**（推荐）| `python3 run_designer.py --web` | 跨平台浏览器，含回放/审核 Tab |
| DearPyGui 桌面版（legacy）| `python3 run_designer.py` | 保留的桌面原型，功能不再扩展 |

## 目录结构

```
osa-rig/
├── README.md                  ← 你现在看的文件
├── run_designer.py            主入口
├── requirements.txt
├── .gitignore
│
├── docs/                      设计与协议文档（给 reviewer 看）
│   ├── architecture.md        系统总览 & 线程/数据流模型
│   ├── block_a_pipeline.md    Block A 状态机 & 时间轴示例
│   ├── snore_detection.md     YAMNet 后端 / 启发式回退 / 调参指南
│   ├── sound_strategies.md    5 种声学策略 & 合成参数
│   ├── data_format.md         sessions/<id>/ 里每个文件是什么
│   ├── airpods_duplex.md      单耳机收放音方案分析
│   ├── roadmap.md             Block B / OSA 事件检测 / PPO 远期路线图
│   └── protocols/
│       ├── chestband_1a2.md   1A2.0 数据帧格式速查
│       └── oximeter_pc68b.md  PC-68B（可选）协议笔记
│
├── devices/                   BLE 驱动层
│   ├── chestband.py           胸带 BleakClient 封装
│   ├── chestband_protocol.py  1A2.0 帧解析
│   └── oximeter.py            PC-68B 驱动
│
├── pipeline/                  核心流水线（独立于 UI）
│   ├── events.py              发布-订阅 EventBus
│   ├── sensors.py             抽象 Sensor 协议
│   ├── posture.py             姿态分类 + debounce
│   ├── snore.py               启发式鼾声检测（后备）
│   ├── snore_yamnet.py        YAMNet 鼾声检测（主力）
│   ├── audio.py               AudioSink（含单耳机 duplex hook）
│   ├── recorder.py            SessionRecorder
│   └── controller.py          Block A 闭环控制器（状态机）
│
├── sounds/                    声学策略合成
│   ├── generator.py           粉噪声 / 滤波 / 包络 / AM
│   ├── spatializer.py         ITD / ILD 双耳空间化
│   └── strategies.py          P1/P2/P3/L1/L2 定义
│
├── server/                    Web 控制台
│   ├── runtime.py             OsaRuntime 单例（硬件 + 会话 + 控制器）
│   └── app.py                 FastAPI REST + WebSocket
│
├── web/                       单页 Vue 应用（无编译）
│   ├── index.html
│   ├── app.js
│   └── styles.css
│
├── ui/                        桌面版（legacy，保留兼容）
│   └── designer.py
│
├── scripts/                   离线工具
│   ├── tune_snore.py          用真实 WAV 调鼾声阈值
│   ├── analyze_night.py       一晚数据汇总 → 策略对比表
│   └── test_chestband.py      胸带解析自测
│
├── dist/                      给非技术被试的一键分发包
│   ├── setup.command
│   ├── preflight_check.command
│   ├── start_night.command
│   ├── end_night.command
│   └── README.md
│
└── tests/                     基础 smoke 测试
    └── test_smoke.py
```

> 数据目录（运行时才产生、不进 git）：`sessions/` `output/` `export/`。

## 架构关键点

- **事件总线驱动**，所有传感器原始数据、姿态变化、鼾声状态、干预决策、
  响应都走 `pipeline/events.py::EventBus`，SessionRecorder 订阅所有事件落盘，
  控制器订阅感兴趣的事件做决策。详见 [`docs/architecture.md`](docs/architecture.md)。
- **线程模型**：BLE 跑在单独的 asyncio 事件循环线程；YAMNet 在 sounddevice
  回调线程收集音频、worker 线程做 TF 推理；FastAPI + WebSocket 由 uvicorn
  主线程处理。EventBus handler 可能在任意线程触发，因此所有可变状态都用锁
  或只做 append 操作。
- **硬件抽象**：`pipeline/sensors.py` 的 `Sensor` 协议和 `pipeline/audio.py` 的
  `AudioSink` 是刻意抽象好的，将来换成耳机平台 SDK 只需实现 `HeadsetAudioSink`，
  控制器代码零改动。
- **Block A 状态机**：详见 [`docs/block_a_pipeline.md`](docs/block_a_pipeline.md)
  的状态表和时间轴示例。
- **数据格式**：详见 [`docs/data_format.md`](docs/data_format.md)；所有触发
  事件的 ±N 秒多通道快照在 `sessions/<id>/events/` 下。

## 远期 roadmap

短名单（等 Block A 数据足够后优先级排序）：

1. **OSA 事件检测** · 从 RIP + SpO2 直接检测 Apnea/Hypopnea（当前系统只做了
   "打鼾"代理）。见 [`docs/roadmap.md`](docs/roadmap.md)。
2. **Block B · 微唤醒截断** · 利用 L1/L2 在鼾声簇形成时打断气道震荡，不求翻身。
3. **个性化策略** · 积累足够多 `(s_t, a_t, r_t, s_{t+1})` 元组后再考虑 PPO。

## License

（暂未指定；内部研究原型。）

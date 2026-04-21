# OSA 打鼾干预实验 · 家用版使用说明

这份包给家用场景准备，非技术背景的被试/研究员可以跟着这份照着做。全程只需要双击 `.command` 文件。

---

## 一、你手头应该有什么

- 一台 **装了 macOS 13+ 的 Mac**（推荐 Apple Silicon / M 系；Intel Mac 能跑但慢）
- 一副 **AirPods / AirPods Pro**（或任意蓝牙耳机 → 负责**放干预音**）
- 一条 **HSR-1A2.0 胸带**（BLE 连 Mac → 采集姿态 + 呼吸 + SpO2）
- 一个 **PC-68B 血氧仪**（可选，BLE 连 Mac → 额外冗余 SpO2）
- **电源 / 充电线**：整晚插电运行
- 这份 zip 包解压后的整个文件夹

---

## 二、一次性安装（**只做一次**，大约 10 分钟）

1. **解压** `osa-rig-vX.Y.zip` 到任何位置，例如桌面。
2. 打开解压后的文件夹，进入 `dist/` 子文件夹。
3. **双击** `setup.command`。
4. 如果弹出 "无法打开，因为不是 App Store 下载的…" 的提示：
   - 右键点该文件 → **打开** → 再次确认。（macOS 会记住，下次双击就行。）
5. 终端窗口会跳出来，按回车键同意，然后**等它跑完**。流程：
   - 检测 Python（没有就提示你装）
   - 创建本地 `venv/`
   - 下载所有依赖（TensorFlow 这个包比较大，首次要 5–10 分钟）
   - 预热 YAMNet 深度学习模型
   - 列出你电脑上的音频设备
6. 看到 `✅ 安装完成!` 就可以关掉窗口。

---

## 三、每晚正式开始前（3 分钟）

1. **配对蓝牙**：
   - 右上角蓝牙图标 → 确认 **AirPods 已连接**
   - 胸带和血氧仪的 BLE 不用在系统里配对，程序里直接扫就行
2. **macOS 声音设置**（⚠️ 重要）：
   - 左上角苹果菜单 → **系统设置 → 声音**
   - **输入** 必须选 "**MacBook 麦克风**"（不要选 AirPods）
   - 输出随意（我们在程序里会自己指定 AirPods）
   - 理由：蓝牙耳机同时做输入+输出会被拉到 HFP 单声道，方向干预音就失效了
3. **预检**：双击 `preflight_check.command`，会：
   - 枚举音频设备
   - 验证 YAMNet 能加载
   - 从扬声器放 3 声"叮"
4. **开始**：双击 `start_night.command`
   - 会自动打开浏览器 `http://localhost:8000`
   - 这个终端窗口**整夜保持打开**（可以最小化）

---

## 四、在浏览器里做的事（首次会用 2 分钟）

### 设备连接 tab

- **胸带**：点「扫描」→ 列表里选中名字含 **HSR / 1A2 / SRG** 的那条 → 点「连接」
- **血氧仪**：同样方式
- **音频通道**：
  - 鼾声输入 = **MacBook Air 麦克风**
  - 干预音输出 = **AirPods**（如果没出现，过几秒自动会刷新）

### 实时实验 tab

- **实验模式**：选 **Block A · 体位干预**（Block B 还没上线）
- 填入 **被试 ID** 和 **备注**
- （可选）底部「活跃时段」设一个时间段，比如 `01:00 → 04:30`，这样只在这段时间干预
- 点 **开始会话** → 顶上状态 banner 会显示 5 条触发条件，全部变绿就代表系统在盯着
- 戴好 AirPods，关灯上床

---

## 五、第二天早上（3 分钟）

1. 起床后，回到那个还开着的终端窗口，按 **Ctrl-C** 关掉（也可以直接关窗口）。
2. 双击 `end_night.command`。
3. 它会自动：
   - 结束当前会话
   - 跑一遍分析脚本（`scripts/analyze_night.py`）
   - 把今晚的 `sessions/<日期>/` 整个目录打包成 zip
   - 自动弹出 `export/` 文件夹
4. **把里面那个 `osa-data-xxx.zip` 发给研究员**（微信 / 飞书云盘 / AirDrop 都可以）。

---

## 六、网页上看到的东西怎么理解

- **顶部 5 个彩色 badge**：同时全部绿 = 刚好 armed，持续 8 秒会播一次干预音。
- **顶部状态背板**：灰=待机；绿=条件满足中；红闪=正在播放；蓝=观察期；深灰=冷却。
- **鼾声判决时间线**：蓝线=打鼾概率，绿色阴影=判为打鼾的时段，橙色虚线=阈值。
- **回放 / 审核 tab**：打完一晚，这里能查每次干预的前后 60 秒：胸呼吸波、SpO2、鼾声概率、干预音、麦克风录音，一目了然。

---

## 七、出了问题怎么办

| 症状 | 应对 |
|------|------|
| 浏览器打不开 `localhost:8000` | 回到 start_night 的终端，看有没有报错；关掉再双击一次 |
| AirPods 没出现在输出下拉里 | 左上角蓝牙菜单点一下 AirPods → 回到网页点「重新枚举设备」 |
| 胸带连不上 / 断线 | 「设备连接」tab 点断开再扫描重连 |
| 顶部 badge `检测到打鼾` 一直不亮 | 对着 MacBook 麦克风放一段打鼾录音，看概率；不行就降低阈值 |
| 自动触发一直没发生 | 顶部 5 个 badge 逐个检查；会话没开始、活跃时段外、都会阻断 |
| 装包失败 | 打开 Terminal → `cd <解压路径>` → 手动跑 `bash dist/setup.command` 看报错 |

---

## 八、数据是什么样子（给研究员看的）

`osa-data-<日期>.zip` 解压后：

```
<日期>/
├── meta.json                会话元信息 (subject, mode, config)
├── summary.json             简短统计
├── events.jsonl             所有事件 (每行一条)
├── interventions.jsonl      每次干预记录 (block/strategy/direction/…)
├── chestband.csv            每秒 vitals 汇总
├── chestband_####.npz       胸带原始波形分块 (每块 ~2s)
├── oximeter.csv             血氧仪时间序列
├── events/
│   ├── YYYYMMDD_HHMMSS_A_P2_left.npz    ±30s 多通道快照 (胸/血氧/鼾概率)
│   ├── YYYYMMDD_HHMMSS_A_P2_left_mic.wav  ±10s 麦克风录音
│   └── YYYYMMDD_HHMMSS_A_P2_left_played.wav  当次播放的干预音副本
└── report/
    ├── summary.json         聚合统计
    ├── strategy_report.csv  每次一行, 包含成功标签 / 潜伏 / SpO2 对比
    └── strategy_report.md   人看的汇总表
```

---

## 九、从研究员角度：接收数据后怎么看

```bash
# 解压到 sessions/ 下
cd <本仓库>/sessions && unzip ~/Downloads/osa-data-xxx.zip

# 重跑/复跑分析
cd <本仓库> && python3 scripts/analyze_night.py sessions/<日期>

# 在本地起 web, 去回放 tab 看每个触发的多通道波形 + 音频
python3 run_designer.py --web
```

---

有 bug / 想改参数, 联系研究员。

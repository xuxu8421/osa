# PC-68B 血氧仪 · 角色说明

## 在本系统里的角色：**必开**，但**不要单独连**

经过 `scripts/_quick_chestband.py` 三组对照实验（见
`docs/protocols/chestband_1a2.md` 里的表），结论非常明确：

- **HSRG 版胸带没有自己的 PPG**，`spo2_pct / pulse_rate / spo2_wave`
  三个字段完全来自 **配对好的 PC-68B 转发**。
- **因此：实验时 PC-68B 必须开机、夹在手指上**，否则胸带数据包里这三列全是 0。
- **但是不需要再走一条独立的 BLE 链路**去连 PC-68B——胸带那条 BLE 已经把数据带过来了。

代码层面，独立连 PC-68B 的旧路径（`devices/oximeter.py` + 调试 HEX 面板）已
**全部移除**：当前主流程里只用胸带 BLE 转发的 SpO2/PR/PPG。如果以后真的要
换成"带自己 PPG 的胸带 + PC-68B 做第二路冗余"，再把驱动从 git 历史里翻出来。

## 配对一次就行

PC-68B 第一次上手要与胸带"绑定"过：

1. 胸带和 PC-68B 同时开机；
2. 按厂商 App 的"设备绑定"流程走一次；
3. 之后只要 PC-68B 一开机、胸带在工作，数据就自动走胸带的 BLE 发到 Mac。

如果换血氧仪、换胸带，需要重新绑定一次。

## UI / 后端表现

- Web UI 设备连接 tab 里 **不**暴露独立的"血氧仪 PC-68B"连接卡片。
- 胸带卡片下方的提示文字说明 "SpO2 / 脉率来自 PC-68B 转发"。
- 顶部状态条 SpO2 badge：
  - 绿色 `SpO2 97%` = 正常；
  - 黄色 `SpO2 失效 (查 PC-68B)` = 超过 5 s 没收到新 SpO2；
  - 灰色 `SpO2 —` = 胸带未连接，谈不上 SpO2。
- snapshot 里给胸带的字段：
  - `spo2_source`: `"relay_pc68b"`
  - `spo2_stale`: `bool`（>5 s 无更新置 True）
  - `spo2_age_s`: 最后一次有效 SpO2 距今多少秒

## USB 离线导出（另一件事）

`scripts/pc68b_usb.py` 跟 BLE 实时链路无关。PC-68B 插上 USB 会像 U 盘一样
挂载，脚本把设备里存的 **历史回放文件** 拷出来。本实验主流程里用不到，只
是留着备用。

## 快速自检

顶部 SpO2 badge 长期黄色/灰色时：

1. 手指真的夹在 PC-68B 的夹子里吗？
2. PC-68B 开着吗？电池还能撑多久？
3. 跑 `python3 scripts/_quick_chestband.py`，看 PPG 波形是不是 `min=0 max=0`
   —— 如果是，就是 PC-68B 没在发数据；如果 PPG 在动但 SpO2 仍然是 0，那是
   测量质量问题（例如信号太弱）。

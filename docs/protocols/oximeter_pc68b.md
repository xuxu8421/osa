# PC-68B 血氧仪 · 角色说明（2026-04-21 实测更新）

## 在本系统里的角色：**必开**，但**不要单独连**

经过 `scripts/_quick_chestband.py` 三组对照实验（见
`docs/protocols/chestband_1a2.md` 里的表），结论非常明确：

- **HSRG 版胸带没有自己的 PPG**，`spo2_pct / pulse_rate / spo2_wave`
  三个字段完全来自 **配对好的 PC-68B 转发**。
- **因此：实验时 PC-68B 必须开机、夹在手指上**，否则胸带数据包里这三列全是 0。
- **但是不需要再走一条独立的 BLE 链路**去连 PC-68B——胸带那条 BLE 已经把数据带过来了。

## 配对一次就行

PC-68B 第一次上手要与胸带"绑定"过。做法通常是：

1. 胸带和 PC-68B 同时开机；
2. 按厂商 App 的"设备绑定"流程走一次；
3. 之后只要 PC-68B 一开机、胸带在工作，数据就自动走胸带的 BLE 发到 Mac。

如果换血氧仪、换胸带，需要重新绑定一次。

## UI / 后端表现

- Web UI 设备连接 tab 里 **不再**暴露独立的"血氧仪 PC-68B"连接卡片（避免两条蓝牙链路抢资源）。
- 胸带卡片下方的提示文字已经写明 "SpO2 / 脉率来自 PC-68B 转发"。
- 顶部状态条 SpO2 badge：
  - 绿色 `SpO2 97%` = 正常；
  - 黄色 `SpO2 失效 (查 PC-68B)` = 超过 5 s 没收到新 SpO2（可能 PC-68B 没电、手指没夹紧、没开机）；
  - 灰色 `SpO2 —` = 胸带都没连上，谈不上 SpO2。
- `server/runtime.py` 的 snapshot 里给胸带一组新字段：
  - `spo2_source`: `"relay_pc68b"`（目前只有这一种）
  - `spo2_stale`: `bool`（>5 s 无更新置 True）
  - `spo2_age_s`: 最后一次有效 SpO2 距今多少秒

## 保留的"独立 BLE 路径"（冗余、仅用于调试）

代码里的 `devices/oximeter.py` + 后端 `oxi_scan / oxi_connect / oxi_disconnect`
API 都**保留**，原因：

1. 如果以后换成 **不带 HSRG 变体的胸带**（真的有自己 PPG 的版本），可能想把
   PC-68B 再当作第二路 SpO2 做一致性校验。
2. 抓包 / 私有协议盲试 —— 这部分仍然挂在 Web UI "调试" tab 里，
   但已经加了提醒："不用在这里连 PC-68B"。

默认情况下这条路径**不会自动启动**，只在用户点击调试 tab 的"发送 HEX"或者手动走
`/api/ble/oxi/connect` 才会建立第二条 BLE 连接。

## USB 离线导出（另一件事）

`scripts/pc68b_usb.py` 跟 BLE 实时链路无关。PC-68B 插上 USB 会像 U 盘一样挂载，
脚本把设备里存的 **历史回放文件** 拷出来。本实验主流程里用不到，只是留着备用。

## 快速自检

如果某天发现顶部 SpO2 badge 长期黄色/灰色：

1. 手指真的夹在 PC-68B 的夹子里吗？（finger_out 是最常见原因）
2. PC-68B 开着吗？电池还能撑多久？
3. 跑一下 `python3 scripts/_quick_chestband.py`，看 PPG 波形是不是
   `min=0 max=0` —— 如果是，就是 PC-68B 没在发数据；如果 PPG 在动但 SpO2=0
   仍然是 0，那是测量质量问题（例如信号太弱）。

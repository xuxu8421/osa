# HSR 1A2.0 胸带 · BLE 数据帧速查

> 本文档是实现方 `devices/chestband_protocol.py` 的对照笔记，方便 review
> 时理解代码在解什么帧。完整版协议请以《1A2.0 无线数据传输协议说明 V1.05》
> 为准。

## BLE 特征

| 名称 | UUID | 方向 | 用途 |
|------|------|------|------|
| NOTIFY | 6e400003-... | 设备 → 主机 | 数据帧 (通知模式) |
| WRITE | 6e400002-... | 主机 → 设备 | 注册响应 / RTC 设置 |

（具体 UUID 见 `devices/chestband.py::SERVICE_UUID / NOTIFY_UUID / WRITE_UUID`）

## 顶层帧格式

```
+------+------+------+------+------+------+---...---+------+
| HEADER (2) | LENGTH (2) | DEVICE_ID (4) | TYPE (1) | PAYLOAD   | CHECKSUM (1) |
+------+------+------+------+------+------+---...---+------+
```

- HEADER：固定 `0x55 0xAA`
- LENGTH：big-endian uint16，包含从 LENGTH 字段到 CHECKSUM 的总字节数
- DEVICE_ID：4 字节设备唯一码
- TYPE：帧类型，常见值：

| 值 | 符号 | 含义 |
|----|------|------|
| 0x01 | FT_REGISTER | 设备上线注册请求 |
| 0x02 | FT_REGISTER_RESP | 主机回复（同意接入） |
| 0x03 | FT_DATA | 数据帧（见下） |
| 0x09 | FT_RTC_SET | 主机下发时间同步 |
| 0x0B | FT_STATUS_CTRL | 设备状态上报 |
| 0x0C | FT_UNREGISTER | 设备下线 |
| 0x0D | FT_BLE_CTRL | BLE 控制 |

- CHECKSUM：payload 全部字节累加求模 0x100

## 数据帧 (TYPE=0x03)

每秒上报 4 个子包。`PacketParser` 内部有一个 `dict[packet_sn, DataPacket]`
做拼装，收齐 4 个子包后合并为一个 `DataPacket` 通过回调送出。

### sub-packet 0：胸呼吸 + ECG 通道 1-2

- chest_resp：25 个 int16（25 Hz 胸呼吸幅值）
- ecg_ch1：50 个 int16（10-bit 有效）
- ecg_ch2：50 个 int16

### sub-packet 1：ECG 通道 3-4 + 腹呼吸

- ecg_ch3, ecg_ch4：各 50 个 int16
- abd_resp：25 个 int16

### sub-packet 2：加速度 + SpO2 + vitals

- accel_x/y/z：各 25 个 int16 (10-bit)
- spo2_wave：50 个 uint8（D7 是脉搏旗标，D6-D0 是 7-bit 波形值）
- 呼吸系数、SpO2 signal strength
- **VitalSigns**：
  - spo2_pct（%）
  - pulse_rate, heart_rate（bpm）
  - resp_rate（次/min）
  - gesture（姿态字节，后由 `PostureAnalyzer` 解析）
  - temperature（℃ × 10 或 int16）
  - battery_voltage_mv
  - device_status

> **⚠️ 实测结论（2026-04-21，HSRG_11003057）**：
> 这个 "HSRG" 变体固件 **自身没有光学 PPG 模块**。`spo2_pct / pulse_rate /
> spo2_wave` 三个字段 **完全来自配对的 PC-68B 血氧仪转发**。
>
> 三次对照实验（`scripts/_quick_chestband.py`），唯一变量是 PC-68B 是否开机：
>
> | 场景 | SpO2 | PR | PPG 波形 |
> |------|:----:|:--:|:--------:|
> | 胸带未戴 + PC-68B 关 | 0 | 0 | 0 |
> | 胸带戴着 + PC-68B 开 (夹手指) | 97% | 85 | 正常波动 |
> | 胸带戴着 + PC-68B 关 | 0 | 0 | 0 |
>
> 推论：
> 1. PC-68B 只需要 **开机夹手指**，数据就经胸带 BLE 进来，**不需要** 再单独 BLE 连它。
> 2. finger_out / 电量耗尽 → 这三个字段回到 0。`server/runtime.py` 加了
>    `spo2_stale` 标记（>5s 无新值置灰）。
> 3. 如果以后换成**带自己 PPG** 的胸带（不带 HSRG 后缀），行为可能不同，
>    届时重新跑 `_quick_chestband.py` 对比判断。

> **⚠️ HSRG 固件另外两个"不算"的字段**：
> - `resp_rate` 永远 0 → 上层用 `_estimate_rr()` 从 `chest_resp` 波形峰检测。
> - `gesture` 永远 0 → `PostureAnalyzer` 直接用 accel 三轴算姿态。
> - `battery_voltage_mv` 隔秒 0 / 3865 交替 → `_on_chest_data` 只接受 >0 的新值。

### sub-packet 3：肺活量计（可选，某些固件才有）

## 主机侧握手

设备上线后会发 FT_REGISTER 帧，主机必须在短时间内回复
`build_register_response(device_id)`，否则设备会反复重试最后退下去。
Bleak 的 `start_notify` 启动后我们在 `ChestBandBLE.start_receiving` 里挂了
`on_registration` 回调，自动写回注册响应 + RTC 时间。

## 姿态字节 → 语义

胸带的 `gesture` 字节是厂家定义的 enum，`pipeline/posture.py` 里有一张映射
表把它翻译成我们用的 `{supine, prone, left, right, upright, unknown}`。
debounce 逻辑也在 PostureAnalyzer 里，避免单帧抖动。

## 常见问题

- **丢包**：sub-packet 收不齐时 `DataPacket.complete == False`，我们会跳过
  这个 `packet_sn` 直到下一秒重新开始拼装。
- **checksum 失败**：会静默丢弃这一帧；极少出现。
- **设备闪断**：Bleak 回调检测到断开后，OsaRuntime 会把 `_ble_state`
  置为 `error`，UI 顶部 badge 变红，等待用户点"断开 → 扫 → 连接"。

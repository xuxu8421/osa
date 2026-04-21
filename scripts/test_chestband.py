#!/usr/bin/env python3
"""
Test script: scan -> connect -> receive -> print parsed chest band data.

Usage:
  python3 scripts/test_chestband.py              # auto-scan for chest band
  python3 scripts/test_chestband.py --scan       # scan only, list devices
  python3 scripts/test_chestband.py --all        # include no-name devices
  python3 scripts/test_chestband.py --addr XX:XX # connect to specific address
"""

import asyncio
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from devices.chestband import ChestBandBLE
from devices.chestband_protocol import DataPacket

_pkt_count = 0


def on_data(dp: DataPacket):
    global _pkt_count
    _pkt_count += 1

    print(f"\n{'='*60}")
    print(f"[#{_pkt_count}]  SN={dp.packet_sn}  设备ID=0x{dp.device_id:08X}  "
          f"时间={dp.timestamp}.{dp.timestamp_ms:03d}")

    v = dp.vitals
    print(f"  SpO2: {v.spo2_pct}%   脉率: {v.pulse_rate} bpm   "
          f"呼吸率: {v.resp_rate}   姿态: {v.gesture}")
    print(f"  电池: {v.battery_voltage_mv} mV   "
          f"体温: {v.temperature:.1f}C")

    if dp.chest_resp is not None:
        print(f"  胸呼吸: min={dp.chest_resp.min()} max={dp.chest_resp.max()} "
              f"(系数={dp.chest_resp_coeff})")
    if dp.abd_resp is not None:
        print(f"  腹呼吸: min={dp.abd_resp.min()} max={dp.abd_resp.max()} "
              f"(系数={dp.abd_resp_coeff})")
    if dp.ecg_ch1 is not None:
        print(f"  ECG ch1: min={dp.ecg_ch1.min()} max={dp.ecg_ch1.max()}")
    if dp.accel_x is not None:
        print(f"  加速度: X=[{dp.accel_x.min()},{dp.accel_x.max()}] "
              f"Y=[{dp.accel_y.min()},{dp.accel_y.max()}] "
              f"Z=[{dp.accel_z.min()},{dp.accel_z.max()}]")
    if dp.spo2_wave is not None:
        wave_vals = dp.spo2_wave & 0x7F
        print(f"  SpO2波形: min={wave_vals.min()} max={wave_vals.max()}")


async def scan_only(all_devs: bool = False):
    pairs = await ChestBandBLE.scan(
        timeout=8.0,
        named_only=not all_devs,
        chestband_only=False,
    )
    print(f"\n找到 {len(pairs)} 个 BLE 设备:\n")
    for i, (d, rssi) in enumerate(pairs):
        name = d.name or '(未知)'
        print(f"  [{i:2d}]  {rssi:>4} dBm  {d.address}  {name}")
    return pairs


async def run(addr: str = None):
    if addr:
        from bleak import BleakScanner
        device = await BleakScanner.find_device_by_address(addr, timeout=10.0)
        if not device:
            print(f"未找到设备: {addr}")
            return
    else:
        print("自动搜索胸带设备...\n")
        matches = await ChestBandBLE.scan(
            timeout=10.0, named_only=True, chestband_only=True)
        if not matches:
            print("\n未找到胸带, 列出所有有名字的设备:")
            all_pairs = await scan_only(all_devs=False)
            if not all_pairs:
                print("无设备")
                return
            print("\n请输入编号(回车退出): ", end='', flush=True)
            try:
                line = await asyncio.get_event_loop().run_in_executor(
                    None, sys.stdin.readline)
                device = all_pairs[int(line.strip())][0]
            except (ValueError, IndexError):
                print("退出")
                return
        else:
            print(f"\n找到 {len(matches)} 个胸带:")
            for i, (d, rssi) in enumerate(matches):
                print(f"  [{i}]  {rssi:>4} dBm  {d.address}  {d.name}")
            if len(matches) == 1:
                device = matches[0][0]
                print(f"\n自动选择: {device.name}")
            else:
                print("\n编号: ", end='', flush=True)
                line = await asyncio.get_event_loop().run_in_executor(
                    None, sys.stdin.readline)
                try:
                    device = matches[int(line.strip())][0]
                except (ValueError, IndexError):
                    print("退出")
                    return

    band = ChestBandBLE()
    await band.connect(device)
    await band.start_receiving(on_data)

    print("接收数据中... Ctrl+C 停止\n")
    try:
        while band.client and band.client.is_connected:
            await asyncio.sleep(1.0)
    except KeyboardInterrupt:
        print("\n\n停止接收")
    finally:
        await band.disconnect()
        print(f"\n共收到 {_pkt_count} 个完整数据包")


def main():
    parser = argparse.ArgumentParser(description='HSR 1A2.0 胸带数据测试')
    parser.add_argument('--scan', action='store_true', help='仅扫描设备')
    parser.add_argument('--all', action='store_true', help='包含无名字的设备')
    parser.add_argument('--addr', type=str, help='直接连接指定地址')
    args = parser.parse_args()

    if args.scan:
        asyncio.run(scan_only(all_devs=args.all))
    else:
        asyncio.run(run(args.addr))


if __name__ == '__main__':
    main()

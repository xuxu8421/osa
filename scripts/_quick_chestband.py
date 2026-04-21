#!/usr/bin/env python3
"""Scan, pick HSRG chest band, connect and print SpO2 + vitals for 30s."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bleak import BleakScanner
from devices.chestband import ChestBandBLE
from devices.chestband_protocol import DataPacket

_n = 0


def on_data(dp: DataPacket):
    global _n
    _n += 1
    v = dp.vitals
    wave_info = ""
    if dp.spo2_wave is not None:
        w = dp.spo2_wave & 0x7F
        wave_info = f"  波形[min={w.min():3d} max={w.max():3d} mean={int(w.mean()):3d}]"
    chest_info = ""
    if dp.chest_resp is not None:
        chest_info = f"  胸呼吸[{int(dp.chest_resp.min())}–{int(dp.chest_resp.max())}]"
    accel_info = ""
    if dp.accel_x is not None:
        ax, ay, az = dp.accel_x, dp.accel_y, dp.accel_z
        accel_info = (f"  加速度 X[{int(ax.min())}–{int(ax.max())}] "
                      f"Y[{int(ay.min())}–{int(ay.max())}] "
                      f"Z[{int(az.min())}–{int(az.max())}]")
    ecg_info = ""
    if dp.ecg_ch1 is not None:
        ecg_info = f"  ECG1[{int(dp.ecg_ch1.min())}–{int(dp.ecg_ch1.max())}]"
    print(f"[#{_n:03d} sn={dp.packet_sn}]  "
          f"SpO2={v.spo2_pct:3d}%  PR={v.pulse_rate:3d}  "
          f"RR={v.resp_rate:3d}  姿态={v.gesture}  "
          f"电池={v.battery_voltage_mv}mV"
          f"{wave_info}{chest_info}{accel_info}{ecg_info}",
          flush=True)


async def main():
    print("扫描 10 秒 ...", flush=True)
    devs = await BleakScanner.discover(timeout=10.0, return_adv=True)
    target = None
    for addr, (dev, adv) in devs.items():
        name = (adv.local_name or dev.name or '').upper()
        if 'HSR' in name or '1A2' in name or 'SRG' in name:
            target = dev
            print(f"候选：{dev.name}  {dev.address}  rssi={adv.rssi}", flush=True)
            break
    if not target:
        print("未找到胸带, 退出", flush=True)
        return

    band = ChestBandBLE()
    await band.connect(target)
    await band.start_receiving(on_data)
    print("\n收数据 30 秒 ...\n", flush=True)
    for _ in range(30):
        await asyncio.sleep(1.0)
    await band.disconnect()
    print(f"\n结束, 共 {_n} 包", flush=True)


if __name__ == '__main__':
    asyncio.run(main())

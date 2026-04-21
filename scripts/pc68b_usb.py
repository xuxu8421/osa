#!/usr/bin/env python3
"""
Detect and dump data from a PC-68B pulse oximeter connected via USB.

Behavior:
  * Scans /Volumes for a newly-mounted removable disk that looks like a
    PC-68B ("Oximeter", "Creative", "PC-68", etc.).
  * If found: lists files, copies them into  ./output/pc68b_usb_<ts>/,
    and prints a head/hexdump of each so we can identify the format.
  * If no such disk exists: lists /dev/tty.usb* and /dev/cu.usb* serial
    devices — PC-68B sometimes exposes itself as a serial port instead.

Usage:
  python3 scripts/pc68b_usb.py            # one-shot scan + dump
  python3 scripts/pc68b_usb.py --watch    # wait for the device to appear
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT  = ROOT / 'output'
OUT.mkdir(exist_ok=True)

NAME_HINTS = ('oximeter', 'creative', 'pc-68', 'pc68', 'pulseox', 'ok-cms',
              'sleepace', 'spo2')


def find_volume() -> Path | None:
    """Find a mounted removable volume whose name hints at an oximeter."""
    vols = Path('/Volumes')
    if not vols.exists():
        return None
    for v in vols.iterdir():
        if v.name.startswith('.'):
            continue
        if any(h in v.name.lower() for h in NAME_HINTS):
            return v
    # Fallback: any small FAT volume (PC-68B disk is typically 32–128 MB)
    for v in vols.iterdir():
        if v.name.startswith('.') or v.name == 'Macintosh HD':
            continue
        try:
            total = shutil.disk_usage(v).total
            if total < 256 * 1024 * 1024:  # < 256 MB, suspicious
                return v
        except Exception:
            continue
    return None


def list_serial_ports() -> list[str]:
    ports = sorted(set(glob.glob('/dev/tty.usb*') + glob.glob('/dev/cu.usb*')))
    return ports


def human_size(n: int) -> str:
    for u in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def head_dump(path: Path, n: int = 256) -> str:
    with open(path, 'rb') as f:
        data = f.read(n)
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hexs = ' '.join(f'{b:02x}' for b in chunk)
        asc = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f"{i:04x}  {hexs:<47s}  {asc}")
    return '\n'.join(lines)


def copy_tree(src: Path, dst: Path) -> list[Path]:
    dst.mkdir(parents=True, exist_ok=True)
    saved = []
    for p in src.rglob('*'):
        if p.is_file() and not p.name.startswith('.'):
            rel = p.relative_to(src)
            out = dst / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(p, out)
                saved.append(out)
            except Exception as e:
                print(f"  copy failed {p}: {e}")
    return saved


def dump_disk(vol: Path):
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    dst = OUT / f'pc68b_usb_{ts}'
    print(f"\n检测到血氧仪磁盘: {vol}")
    try:
        total = shutil.disk_usage(vol).total
        print(f"  卷容量: {human_size(total)}")
    except Exception:
        pass
    print(f"  拷贝到: {dst}")
    saved = copy_tree(vol, dst)
    print(f"  共拷贝 {len(saved)} 个文件\n")

    for p in saved:
        rel = p.relative_to(dst)
        size = p.stat().st_size
        print(f"--- {rel}  ({human_size(size)}) ---")
        if size == 0:
            print("(空文件)\n")
            continue
        try:
            print(head_dump(p, n=512))
        except Exception as e:
            print(f"(无法读: {e})")
        print()

    idx = dst / 'INDEX.txt'
    with open(idx, 'w', encoding='utf-8') as f:
        f.write(f"Source: {vol}\nTime:   {ts}\n\n")
        for p in saved:
            rel = p.relative_to(dst)
            size = p.stat().st_size
            f.write(f"{size:>10}  {rel}\n")
    print(f"索引已写入 {idx}")


def once(watch: bool = False):
    if watch:
        print("等待血氧仪挂载 (Ctrl-C 退出)...")
        while True:
            v = find_volume()
            if v:
                dump_disk(v)
                return
            time.sleep(1.0)
    else:
        v = find_volume()
        if v:
            dump_disk(v)
            return
        print("未发现血氧仪磁盘。")
        print("  /Volumes 下当前挂载:")
        for it in sorted(Path('/Volumes').iterdir()):
            if not it.name.startswith('.'):
                print(f"    - {it.name}")
        ports = list_serial_ports()
        if ports:
            print("\n发现可能的 USB 串口设备:")
            for p in ports:
                print(f"  - {p}")
            print("\n(若 Mac 未自动挂载, 血氧仪可能走串口协议, "
                  "后续我再写一段 pyserial 读取)")
        else:
            print("  也无 /dev/tty.usb* 串口设备。")
            print("  请检查: USB 线接牢、血氧仪屏幕是否显示 '上传中/Uploading'。")


def main():
    ap = argparse.ArgumentParser(description='PC-68B USB 数据导出验证')
    ap.add_argument('--watch', action='store_true',
                    help='持续等待血氧仪 USB 挂载')
    args = ap.parse_args()
    try:
        once(watch=args.watch)
    except KeyboardInterrupt:
        print('\n已退出')
        sys.exit(0)


if __name__ == '__main__':
    main()

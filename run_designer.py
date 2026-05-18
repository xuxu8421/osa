#!/usr/bin/env python3
"""Launch the OSA experiment console (FastAPI + static SPA)."""

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

MY_PID = os.getpid()


def kill_previous_instances():
    """Kill any other python run_designer.py processes still holding BLE."""
    try:
        out = subprocess.check_output(['ps', '-xo', 'pid,command'], text=True)
    except Exception:
        return
    killed = 0
    for line in out.splitlines()[1:]:
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        pid_s, cmd = parts
        if 'run_designer.py' not in cmd:
            continue
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        if pid == MY_PID:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except ProcessLookupError:
            pass
        except Exception as e:
            print(f"  kill {pid} failed: {e}")
    if killed:
        print(f"[run] 杀掉 {killed} 个旧进程, 等 1.5s 释放 BLE...")
        time.sleep(1.5)


def lan_ip_hint() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def spawn_caffeinate() -> Optional[subprocess.Popen]:
    """Spawn macOS `caffeinate` so the experiment can run all night
    even if the user dims/closes the screen.

    Flags:
      -i  prevent idle sleep (the typical "no input for N min → sleep")
      -s  prevent system sleep when on AC power
      -w  expire when our PID dies (no orphan process)

    We deliberately do NOT pass -d (would keep the display awake; the
    subject would have a glowing screen by their bed) or -u (declares
    the user as active; pointless for an unattended overnight run).

    Caveat: closing the laptop lid still triggers Apple's "clamshell
    sleep" which `caffeinate` cannot block. Keep the lid open or
    plug in an external display to defeat it.
    """
    if sys.platform != 'darwin':
        return None
    try:
        proc = subprocess.Popen(
            ['caffeinate', '-is', '-w', str(MY_PID)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        print(f"[run] caffeinate {proc.pid} 已绑到 PID {MY_PID} "
              f"(阻止系统/idle 睡眠; 屏幕仍可变暗 — 整夜实验所需)")
        return proc
    except FileNotFoundError:
        print("[run] caffeinate 不在 PATH (非 macOS?), 跳过省电守护")
    except Exception as e:
        print(f"[run] 启动 caffeinate 失败: {e}")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--web', action='store_true',
                    help='(legacy flag, kept for compatibility — web is the only mode)')
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=8000)
    ap.add_argument('--no-caffeinate', action='store_true',
                    help='不自动启动 macOS caffeinate (默认会启动, 阻止整夜睡眠)')
    args = ap.parse_args()

    kill_previous_instances()

    caff = None if args.no_caffeinate else spawn_caffeinate()

    import uvicorn
    from server.app import app
    print(f"[web] http://localhost:{args.port}   ·   "
          f"同 Wi-Fi:  http://{lan_ip_hint()}:{args.port}")
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level='info',
                    access_log=False)
    finally:
        if caff is not None:
            try:
                caff.terminate()
            except Exception:
                pass


if __name__ == '__main__':
    main()

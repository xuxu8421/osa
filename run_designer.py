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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--web', action='store_true',
                    help='(legacy flag, kept for compatibility — web is the only mode)')
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=8000)
    args = ap.parse_args()

    kill_previous_instances()

    import uvicorn
    from server.app import app
    print(f"[web] http://localhost:{args.port}   ·   "
          f"同 Wi-Fi:  http://{lan_ip_hint()}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level='info',
                access_log=False)


if __name__ == '__main__':
    main()

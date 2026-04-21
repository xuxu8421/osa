#!/bin/bash
# start_night.command — launch the OSA web console, open browser.
# Keep this Terminal window open during the whole night.
# Close it (Ctrl-C or ⌘W) at the end of the experiment.

set -e

HERE="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
ROOT="$(dirname "$HERE")"
cd "$ROOT"

if [ ! -d "$ROOT/venv" ]; then
  printf "× 还没运行过 setup.command\n请先双击 setup.command 完成一次性安装\n"
  read -p "按回车键关闭 …"
  exit 1
fi
source "$ROOT/venv/bin/activate"

printf "\033[1;34m▶ OSA 干预实验控制台\033[0m\n"
printf "   时间: %s\n\n" "$(date)"

# Kill any stray previous instance hogging port 8000 / BLE.
for pid in $(pgrep -f 'run_designer.py' || true); do
  if [ "$pid" != "$$" ]; then
    kill -TERM "$pid" 2>/dev/null || true
  fi
done
sleep 1

# Open the browser a moment after uvicorn comes up.
(
  sleep 3
  open http://localhost:8000 2>/dev/null || true
) &

printf "启动 web 服务, 浏览器会自动打开 http://localhost:8000\n\n"
printf "▶ 全夜进行中…\n"
printf "  · 别关这个窗口\n"
printf "  · 结束时回到这里按 Ctrl-C, 然后双击 end_night.command\n\n"

exec python run_designer.py --web

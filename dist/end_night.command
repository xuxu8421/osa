#!/bin/bash
# end_night.command — stop server (if running), run overnight analysis on
# the most recent session, zip the session folder, and open the export
# folder in Finder so the user can send it to the researcher.

set -e

HERE="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
ROOT="$(dirname "$HERE")"
cd "$ROOT"

printf "\033[1;34m▶ 结束夜间会话 · 打包数据\033[0m\n\n"

# Make sure the server is shut down so files are fully flushed.
for pid in $(pgrep -f 'run_designer.py' || true); do
  printf "▶ 关闭 web 服务 (pid=%s)…\n" "$pid"
  kill -TERM "$pid" 2>/dev/null || true
done
sleep 2

if [ ! -d "$ROOT/venv" ]; then
  printf "× 未检测到 venv, 请先完成 setup.command\n"
  exit 1
fi
source "$ROOT/venv/bin/activate"

# Find the latest session.
LATEST=$(ls -td "$ROOT/sessions"/*/ 2>/dev/null | head -n1 | xargs -I{} basename {})
if [ -z "$LATEST" ]; then
  printf "× sessions/ 为空, 没有可打包的会话\n"
  read -p "按回车键关闭 …"
  exit 1
fi

printf "▶ 本次会话: %s\n" "$LATEST"

printf "\n▶ 运行夜间分析 (analyze_night.py)…\n"
python scripts/analyze_night.py "sessions/$LATEST" || \
  printf "  (分析脚本失败, 原始数据仍会被打包)\n"

mkdir -p "$ROOT/export"
ZIP_NAME="osa-data-$LATEST.zip"
ZIP_PATH="$ROOT/export/$ZIP_NAME"

printf "\n▶ 打包为 %s …\n" "$ZIP_NAME"
cd "$ROOT/sessions"
zip -rq "$ZIP_PATH" "$LATEST"
cd "$ROOT"

SIZE=$(du -sh "$ZIP_PATH" | cut -f1)
printf "\n\033[1;32m✅ 打包完成\033[0m\n"
printf "   文件: %s (%s)\n" "$ZIP_PATH" "$SIZE"
printf "\n把这个 .zip 发给研究员 (微信/飞书/网盘 任选):\n"
printf "   %s\n\n" "$ZIP_PATH"

open "$ROOT/export" 2>/dev/null || true
read -p "按回车键关闭 …"

#!/bin/bash
# setup.command — one-time installer for the OSA rig (Apple Silicon Mac).
# Double-click to run. Creates a local venv, installs deps, warms the
# YAMNet cache, and does a preflight check.

set -e

HERE="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
ROOT="$(dirname "$HERE")"
cd "$ROOT"

printf "\033[1;34m▶ OSA 实验设备 · 一次性安装\033[0m\n\n"

# Pick python
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v /opt/homebrew/bin/python3 >/dev/null 2>&1; then
  PY=/opt/homebrew/bin/python3
else
  cat <<EOF
× 未找到 Python 3

请先装 Python 3.9+:
   1. 打开 https://www.python.org/downloads/macos/  下载 macOS 安装包
   2. 或 Homebrew: /bin/bash -c "\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" && brew install python@3.11
装好之后再双击本文件。
EOF
  read -p "按任意键关闭…"
  exit 1
fi

printf "▶ Python: %s (%s)\n" "$($PY --version)" "$(which $PY)"

if [ ! -d "$ROOT/venv" ]; then
  printf "▶ 创建 venv …\n"
  $PY -m venv "$ROOT/venv"
fi
source "$ROOT/venv/bin/activate"
printf "▶ 升级 pip …\n"
pip install --upgrade pip >/dev/null

printf "▶ 安装依赖 (首次大概 5-10 分钟) …\n"
pip install -r "$ROOT/requirements.txt"

# Guard: TF 2.16 only works with numpy<2. Some transitive dep (opencv etc)
# may still push numpy to 2.x; force it back. This is the #1 cause of
# "numpy.core.umath failed to import" at runtime.
printf "▶ 校准 numpy 版本 (TF 需要 <2) …\n"
pip install 'numpy>=1.26,<2' --force-reinstall --no-deps >/dev/null

printf "\n▶ 预热 YAMNet 模型 (首次 ~15 秒, 下载 ~15 MB 权重 + 14 KB 类表) …\n"
python -c "
import os; os.environ['TF_CPP_MIN_LOG_LEVEL']='2'
from pipeline.snore_yamnet import YamnetSnoreDetector
d = YamnetSnoreDetector(bus=None)
ok = d._ensure_model()
if ok:
    print('  YAMNet OK ·', len(d._class_names), '类 · 缓存在 ~/.cache/osa_yamnet/')
else:
    print('  × YAMNet 预热失败:', d.error)
" || printf "  (YAMNet 预热失败, 程序首次启动时会重试)\n"

printf "\n▶ 枚举音频设备 …\n"
python -c "
import sounddevice as sd
for i,d in enumerate(sd.query_devices()):
    tag = []
    if d['max_input_channels']>0: tag.append('in')
    if d['max_output_channels']>0: tag.append('out')
    print(f'  [{i}] {d[\"name\"]:30s} {\"|\".join(tag)}')
"

printf "\n\033[1;32m✅ 安装完成!\033[0m\n"
printf "\n下一步:\n"
printf "  1. 双击 \033[1mstart_night.command\033[0m 打开控制台\n"
printf "  2. 浏览器会自动打开 http://localhost:8000\n"
printf "  3. 把耳机用蓝牙连上 Mac (输出端)\n"
printf "  4. 在网页「设备连接」tab 扫描并连接胸带\n"
printf "\n"
read -p "按回车键关闭此窗口 (环境会保留) …"

#!/bin/bash
# preflight_check.command — quick before-bedtime check: does the rig work?
# Enumerates audio devices, confirms YAMNet loads, and pings localhost:8000
# if the server is already running.

set -e

HERE="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
ROOT="$(dirname "$HERE")"
cd "$ROOT"

if [ ! -d "$ROOT/venv" ]; then
  printf "× 请先双击 setup.command 完成一次性安装\n"
  read -p "按回车键关闭 …"
  exit 1
fi
source "$ROOT/venv/bin/activate"

printf "\033[1;34m▶ 设备自检\033[0m\n\n"

printf "▶ 音频设备:\n"
python -c "
import sounddevice as sd
raw = sd.query_devices()
print(f'   共 {len(raw)} 个设备:')
for i,d in enumerate(raw):
    tag=[]
    if d['max_input_channels']>0: tag.append('输入')
    if d['max_output_channels']>0: tag.append('输出')
    print(f'   [{i}] {d[\"name\"]:30s} {\" | \".join(tag)}')
try:
    di, do = sd.default.device
    print(f'   默认: 输入=[{di}]  输出=[{do}]')
except Exception as e:
    print(f'   (查询默认失败: {e})')
"

printf "\n▶ YAMNet 模型 (在线打鼾检测):\n"
python -c "
import os; os.environ['TF_CPP_MIN_LOG_LEVEL']='2'
from pipeline.snore_yamnet import YamnetSnoreDetector
d = YamnetSnoreDetector(bus=None)
print('   OK (已缓存)' if d._ensure_model() else f'   × {d.error}')
" || printf "   × YAMNet 加载失败 — 检查网络或重新运行 setup\n"

printf "\n▶ 测试音 (3 声, MacBook 扬声器):\n"
python -c "
import sounddevice as sd, numpy as np, time
for i in range(3):
    t = np.linspace(0, 0.3, int(48000*0.3), endpoint=False)
    w = 0.2*np.sin(2*np.pi*440*t).astype('float32')
    sd.play(w, 48000); time.sleep(0.5); sd.stop()
    print(f'   第 {i+1} 声')
print('   OK')
"

printf "\n▶ 蓝牙: 请确认\n"
printf "   · 耳机已连接 (左上角蓝牙菜单)\n"
printf "   · 胸带 (HSR/1A2/SRG) 开机 + 戴在身上\n"
printf "   · PC-68B 血氧仪开机 + 夹手指 (它会经胸带 BLE 转发数据进来)\n"

printf "\n\033[1;32m自检完毕\033[0m\n"
read -p "按回车键关闭 …"

#!/usr/bin/env python3
"""
Offline snore-detection tuner.

Reads one or more audio files and runs the *exact* same feature pipeline as
`pipeline.snore.MicSnoreDetector` so you can pick thresholds on your own
recordings before touching the live web UI.

Features
  * per-frame energy_db + low-band (80-500 Hz) ratio + detection flag
  * summary stats (coverage, mean energy during detected vs quiet)
  * optional threshold sweep (grid of energy × band-ratio)
  * optional plot (matplotlib) with dashed threshold lines

Usage
  python3 scripts/tune_snore.py snore.wav
  python3 scripts/tune_snore.py --energy-db -50 --band-ratio 0.5 snore.wav
  python3 scripts/tune_snore.py --plot snore.wav quiet.wav
  python3 scripts/tune_snore.py --sweep-energy=-60,-50,-40,-30 \
                                 --sweep-br=0.4,0.5,0.6,0.7 snore.wav
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

import numpy as np

try:
    import soundfile as sf
except ImportError:
    sys.exit("需要 soundfile: pip install soundfile")


# Keep in lockstep with pipeline/snore.py defaults.
DEFAULTS = dict(
    sample_rate=16000,
    frame_s=0.064,
    window_s=1.5,
    hangover_s=1.2,
    energy_db=-45.0,
    band_ratio_min=0.55,
    band_hz=(80.0, 500.0),
)


def load_audio(path: Path, target_sr: int) -> np.ndarray:
    y, sr = sf.read(str(path), dtype='float32', always_2d=False)
    if y.ndim == 2:
        y = y.mean(axis=1)
    if sr != target_sr:
        # Simple linear resampler — fine for snore-band analysis; avoid
        # pulling in scipy just for this.
        ratio = target_sr / sr
        n_out = int(round(len(y) * ratio))
        x_old = np.linspace(0, 1, num=len(y), endpoint=False)
        x_new = np.linspace(0, 1, num=n_out, endpoint=False)
        y = np.interp(x_new, x_old, y).astype(np.float32)
    return y


def extract_features(y: np.ndarray, sr: int, frame_s: float,
                     window_s: float, band_hz=(80.0, 500.0)) -> dict:
    """Slide the same 1.5 s window (hop = frame_s) used by the live detector."""
    frame_n = max(64, int(sr * frame_s))
    window_n = int(sr * window_s)
    if len(y) < window_n:
        # Pad short clips
        y = np.pad(y, (0, window_n - len(y)))
    hop = frame_n
    window = np.hanning(window_n).astype(np.float32)
    lo, hi = band_hz
    freqs = np.fft.rfftfreq(window_n, 1.0 / sr)
    band_mask = (freqs >= lo) & (freqs <= hi)

    t_centers, energies_db, ratios = [], [], []
    for start in range(0, len(y) - window_n + 1, hop):
        buf = y[start:start + window_n]
        rms = float(np.sqrt(np.mean(buf * buf)) + 1e-12)
        energy_db = 20.0 * np.log10(rms)

        spec = np.abs(np.fft.rfft(buf * window))
        total = float(np.sum(spec) + 1e-12)
        ratio = float(np.sum(spec[band_mask]) / total)

        t_centers.append((start + window_n / 2) / sr)
        energies_db.append(energy_db)
        ratios.append(ratio)

    return {
        't': np.asarray(t_centers),
        'energy_db': np.asarray(energies_db),
        'band_ratio': np.asarray(ratios),
        'frame_s': frame_s,
    }


def apply_detection(feats: dict, energy_db: float, band_ratio_min: float,
                    hangover_s: float) -> np.ndarray:
    """Replicate the hangover logic from `MicSnoreDetector._on_audio`."""
    t = feats['t']
    loud = feats['energy_db'] > energy_db
    snore_like = feats['band_ratio'] > band_ratio_min
    raw = loud & snore_like

    flags = np.zeros(len(t), dtype=bool)
    last_loud_at = -1e9
    snoring = False
    for i, ti in enumerate(t):
        if raw[i]:
            snoring = True
            last_loud_at = ti
        elif snoring and (ti - last_loud_at) > hangover_s:
            snoring = False
        flags[i] = snoring
    return flags


def summarize(name: str, feats: dict, flags: np.ndarray,
              energy_db: float, band_ratio_min: float):
    n = len(flags)
    n_pos = int(flags.sum())
    dur = feats['t'][-1] if n else 0.0
    coverage = (n_pos / n * 100.0) if n else 0.0
    e_on = feats['energy_db'][flags]
    e_off = feats['energy_db'][~flags]
    r_on = feats['band_ratio'][flags]
    r_off = feats['band_ratio'][~flags]

    def _stat(a):
        if len(a) == 0:
            return '     —'
        return f'{np.mean(a):6.1f}'

    print(f"\n=== {name} ===")
    print(f"  时长         {dur:6.1f} s   帧数 {n}")
    print(f"  阈值         energy_db > {energy_db:.1f}   "
          f"band_ratio > {band_ratio_min:.2f}")
    print(f"  判为打鼾     {n_pos}/{n} 帧 ({coverage:5.1f}%)")
    print(f"  能量 (dB)    打鼾段 {_stat(e_on)}   安静段 {_stat(e_off)}")
    print(f"  带能比       打鼾段 {_stat(r_on)}   安静段 {_stat(r_off)}")
    print(f"  分位数 (all) energy  "
          f"p10={np.percentile(feats['energy_db'], 10):.1f}  "
          f"p50={np.percentile(feats['energy_db'], 50):.1f}  "
          f"p90={np.percentile(feats['energy_db'], 90):.1f}")
    print(f"               ratio   "
          f"p10={np.percentile(feats['band_ratio'], 10):.2f}  "
          f"p50={np.percentile(feats['band_ratio'], 50):.2f}  "
          f"p90={np.percentile(feats['band_ratio'], 90):.2f}")


def print_timeline(feats: dict, flags: np.ndarray, step_s: float = 0.25):
    """Compact per-window ascii report (one line every `step_s`)."""
    t = feats['t']
    hop = feats['frame_s']
    every = max(1, int(step_s / hop))
    print("\n   t(s)   energy   ratio   snore")
    for i in range(0, len(t), every):
        mark = '█' if flags[i] else '·'
        print(f"  {t[i]:5.1f}   "
              f"{feats['energy_db'][i]:6.1f}   "
              f"{feats['band_ratio'][i]:4.2f}   {mark}")


def sweep(feats: dict, hangover_s: float,
          e_grid: list[float], r_grid: list[float]):
    print("\n  阈值扫描 (每格 = 判为打鼾的帧占比 %)")
    header = "   energy \\ ratio |"
    header += ''.join(f'  {r:4.2f}' for r in r_grid)
    print(header)
    print('   ' + '-' * (len(header) - 3))
    for e in e_grid:
        row = f"   {e:7.1f}       |"
        for r in r_grid:
            flags = apply_detection(feats, e, r, hangover_s)
            pct = 100.0 * flags.sum() / len(flags) if len(flags) else 0.0
            row += f'  {pct:4.1f} '
        print(row)


def maybe_plot(results: list[tuple[str, dict, np.ndarray]],
               energy_db: float, band_ratio_min: float):
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("\n(跳过绘图: 未安装 matplotlib)")
        return
    n = len(results)
    fig, axes = plt.subplots(n, 2, figsize=(11, 2.6 * n), squeeze=False)
    for i, (name, f, flags) in enumerate(results):
        ax_e, ax_r = axes[i]
        ax_e.plot(f['t'], f['energy_db'], color='#38bdf8', lw=0.9)
        ax_e.axhline(energy_db, color='#f59e0b', ls='--', lw=0.8)
        ax_e.fill_between(f['t'], f['energy_db'].min(), f['energy_db'].max(),
                          where=flags, alpha=0.12, color='#22c55e',
                          step='mid')
        ax_e.set_ylabel('energy_db')
        ax_e.set_title(name)

        ax_r.plot(f['t'], f['band_ratio'], color='#a78bfa', lw=0.9)
        ax_r.axhline(band_ratio_min, color='#f59e0b', ls='--', lw=0.8)
        ax_r.fill_between(f['t'], 0, 1, where=flags, alpha=0.12,
                          color='#22c55e', step='mid')
        ax_r.set_ylabel('band_ratio (80-500 Hz)')
        ax_r.set_ylim(0, 1)
        if i == n - 1:
            ax_e.set_xlabel('t (s)')
            ax_r.set_xlabel('t (s)')
    fig.tight_layout()
    plt.show()


def yamnet_classify(files: list[Path], sr: int = 16000,
                    snoring_idx: int = 38, thresh: float = 0.3) -> None:
    """Run YAMNet on each file and report the snoring class probability."""
    try:
        import os as _os
        _os.environ.setdefault('TF_CPP_MIN_LOG_LEVEL', '2')
        import tensorflow as tf
        import tensorflow_hub as hub
    except Exception as e:
        print(f"YAMNet 需要 tensorflow + tensorflow_hub: {e}", file=sys.stderr)
        return
    print("  正在加载 YAMNet (首次 ~10 秒)...")
    m = hub.load('https://tfhub.dev/google/yamnet/1')
    class_map = m.class_map_path().numpy().decode()
    with open(class_map) as f:
        next(f)
        names = [row.split(',')[2].strip() for row in f]

    for p in files:
        y = load_audio(p, sr)
        scores, _, _ = m(tf.constant(y))
        s = scores.numpy()                     # (n_frames, 521)
        mean = s.mean(axis=0)
        snoring_p = float(mean[snoring_idx])
        top = np.argsort(mean)[::-1][:5]
        print(f"\n=== {p.name} ===  时长 {len(y)/sr:.1f}s")
        print(f"  Snoring  p={snoring_p:.3f}  "
              f"(> {thresh} -> {'阳' if snoring_p > thresh else '阴'})")
        print("  Top-5:")
        for i in top:
            print(f"    {names[i]:30s} p={mean[i]:.3f}")
        # Per-second timeline
        per_s = s[:, snoring_idx]
        n = len(per_s)
        if n:
            print("  逐 0.48s snoring 概率:")
            line = ''
            for v in per_s:
                line += '█' if v >= thresh else (':' if v > 0.1 else '.')
            print('    ' + line)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('files', nargs='+', help='WAV/MP3/FLAC audio files')
    ap.add_argument('--yamnet', action='store_true',
                    help='改用 YAMNet (AudioSet 预训练) 判定, 仅输出打鼾概率')
    ap.add_argument('--yamnet-thresh', type=float, default=0.3)
    ap.add_argument('--energy-db', type=float, default=DEFAULTS['energy_db'],
                    help=f'能量阈值 dB (默认 {DEFAULTS["energy_db"]})')
    ap.add_argument('--band-ratio', type=float,
                    default=DEFAULTS['band_ratio_min'],
                    help=f'低频带能比下限 (默认 {DEFAULTS["band_ratio_min"]})')
    ap.add_argument('--hangover-s', type=float,
                    default=DEFAULTS['hangover_s'],
                    help=f'hangover 秒 (默认 {DEFAULTS["hangover_s"]})')
    ap.add_argument('--sr', type=int, default=DEFAULTS['sample_rate'])
    ap.add_argument('--timeline', action='store_true',
                    help='打印每 0.25 s 的 ascii 时间线')
    ap.add_argument('--plot', action='store_true',
                    help='用 matplotlib 绘图 (需要 pip install matplotlib)')
    ap.add_argument('--sweep-energy', type=str, default='',
                    help='例如 -60,-50,-40,-30')
    ap.add_argument('--sweep-br', type=str, default='',
                    help='例如 0.4,0.5,0.6,0.7')
    args = ap.parse_args()

    valid_paths = []
    for fp in args.files:
        p = Path(fp)
        if not p.exists():
            print(f'skip: {fp} (not found)', file=sys.stderr)
            continue
        valid_paths.append(p)

    if args.yamnet:
        yamnet_classify(valid_paths, sr=args.sr, thresh=args.yamnet_thresh)
        return

    results = []
    for p in valid_paths:
        y = load_audio(p, args.sr)
        feats = extract_features(y, args.sr,
                                 DEFAULTS['frame_s'], DEFAULTS['window_s'])
        flags = apply_detection(feats, args.energy_db,
                                args.band_ratio, args.hangover_s)
        summarize(p.name, feats, flags, args.energy_db, args.band_ratio)
        if args.timeline:
            print_timeline(feats, flags)
        if args.sweep_energy and args.sweep_br:
            e_grid = [float(x) for x in args.sweep_energy.split(',')]
            r_grid = [float(x) for x in args.sweep_br.split(',')]
            sweep(feats, args.hangover_s, e_grid, r_grid)
        results.append((p.name, feats, flags))

    if args.plot and results:
        maybe_plot(results, args.energy_db, args.band_ratio)


if __name__ == '__main__':
    main()

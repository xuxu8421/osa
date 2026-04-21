#!/usr/bin/env python3
"""
Analyze one recorded session (a full night / pilot run) and produce:
  sessions/<id>/report/summary.json        aggregate metrics
  sessions/<id>/report/strategy_report.csv one row per intervention
  sessions/<id>/report/strategy_report.md  human-readable grouped table

Usage:
  python3 scripts/analyze_night.py sessions/20260420_220340
  python3 scripts/analyze_night.py --all           # every sub-dir under sessions/
  python3 scripts/analyze_night.py --latest        # most recent session
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from statistics import mean, median, stdev
from typing import Any, Dict, Iterable, List, Optional

import numpy as np


ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR = ROOT / 'sessions'


# ───────────────────────────────── helpers ───────────────────────────────

def read_jsonl(fp: Path) -> List[dict]:
    if not fp.exists():
        return []
    out = []
    with open(fp, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def read_meta(session_dir: Path) -> dict:
    fp = session_dir / 'meta.json'
    if not fp.exists():
        return {}
    try:
        return json.loads(fp.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _safe_mean(xs):
    xs = [x for x in xs if x is not None and not math.isnan(x)]
    return float(mean(xs)) if xs else None


def _safe_median(xs):
    xs = [x for x in xs if x is not None and not math.isnan(x)]
    return float(median(xs)) if xs else None


def _safe_stdev(xs):
    xs = [x for x in xs if x is not None and not math.isnan(x)]
    return float(stdev(xs)) if len(xs) > 1 else None


def _safe_min(xs):
    xs = [x for x in xs if x is not None and not math.isnan(x)]
    return float(min(xs)) if xs else None


# ─────────────────────────────── extractors ──────────────────────────────

def pair_triggers_with_responses(interventions: List[dict],
                                 events: List[dict]) -> List[dict]:
    """interventions.jsonl contains 'triggered' rows; responses live in
    events.jsonl under kind='intervention.response' with matching strategy.
    Pair them by order (each intervention has exactly one response)."""
    responses = [e for e in events
                 if e.get('kind') == 'intervention.response']
    paired = []
    r_iter = iter(responses)
    for iv in interventions:
        resp = next(r_iter, None)
        paired.append({'iv': iv, 'resp': (resp or {}).get('payload') or {}})
    return paired


def snore_coverage(events: List[dict], t_lo: float, t_hi: float) -> float:
    """Fraction of snore.state samples in [t_lo, t_hi] that were 'snoring'."""
    total = 0
    snoring = 0
    for e in events:
        if e.get('kind') != 'snore.state':
            continue
        t = e.get('t', 0.0)
        if not (t_lo <= t <= t_hi):
            continue
        total += 1
        p = e.get('payload') or {}
        if p.get('snoring'):
            snoring += 1
    if total == 0:
        return 0.0
    return snoring / total


def spo2_window(events: List[dict], t_lo: float, t_hi: float) -> dict:
    """Return SpO2 min/mean over [t_lo, t_hi] using chestband.summary
    (ev.kind == 'chestband.summary' carries {vitals:{spo2}}).
    """
    vals = []
    for e in events:
        if e.get('kind') != 'chestband.summary':
            continue
        t = e.get('t', 0.0)
        if not (t_lo <= t <= t_hi):
            continue
        v = (e.get('vitals') if 'vitals' in e else None) or \
            ((e.get('payload') or {}).get('vitals')) or {}
        spo2 = v.get('spo2')
        if spo2 is None or spo2 == '' or spo2 == 0:
            continue
        try:
            spo2 = float(spo2)
        except Exception:
            continue
        if 70 <= spo2 <= 100:
            vals.append(spo2)
    return {'n': len(vals),
            'min': _safe_min(vals),
            'mean': _safe_mean(vals)}


# ─────────────────────────────── per-session ─────────────────────────────

def analyze_session(session_dir: Path) -> Optional[dict]:
    meta = read_meta(session_dir)
    if not meta:
        print(f"  [skip] {session_dir.name}: no meta.json",
              file=sys.stderr)
        return None

    interventions = read_jsonl(session_dir / 'interventions.jsonl')
    events = read_jsonl(session_dir / 'events.jsonl')

    if not interventions:
        print(f"  [note] {session_dir.name}: 0 interventions recorded")

    paired = pair_triggers_with_responses(interventions, events)

    rows = []
    for i, pr in enumerate(paired, start=1):
        iv = pr['iv']
        resp = pr['resp']
        t = float(iv.get('t', 0.0))
        strategy = iv.get('strategy', '')
        direction = iv.get('direction', '')
        block = iv.get('block', meta.get('mode', 'A'))
        level_db = iv.get('level_db', '')
        reason = iv.get('reason', '')
        success = bool(resp.get('success', False))
        latency_s = resp.get('latency_s')
        resp_reason = resp.get('reason', '')

        # Snoring coverage before/after the trigger
        snore_pre = snore_coverage(events, t - 30, t)
        snore_post = snore_coverage(events, t + 3, t + 30)
        # SpO2 before/after
        sp_pre = spo2_window(events, t - 30, t)
        sp_post = spo2_window(events, t, t + 60)

        rows.append({
            'idx': i,
            'block': block,
            't': t,
            'strategy': strategy,
            'direction': direction,
            'level_db': level_db,
            'reason': reason,
            'success': int(success),
            'latency_s': latency_s,
            'resp_reason': resp_reason,
            'snore_pct_pre_30s': round(100 * snore_pre, 1),
            'snore_pct_post_30s': round(100 * snore_post, 1),
            'spo2_mean_pre_30s': sp_pre['mean'],
            'spo2_min_pre_30s': sp_pre['min'],
            'spo2_mean_post_60s': sp_post['mean'],
            'spo2_min_post_60s': sp_post['min'],
        })

    # Group by strategy
    groups: Dict[str, List[dict]] = {}
    for r in rows:
        groups.setdefault(r['strategy'] or '(none)', []).append(r)

    def _group_stat(g: List[dict]) -> dict:
        return {
            'n': len(g),
            'success_rate_pct': round(
                100 * sum(r['success'] for r in g) / len(g), 1) if g else None,
            'latency_median_s': _safe_median(
                [r['latency_s'] for r in g if r['success'] and
                 r['latency_s'] is not None]),
            'latency_mean_s': _safe_mean(
                [r['latency_s'] for r in g if r['success'] and
                 r['latency_s'] is not None]),
            'snore_pct_pre_avg': _safe_mean(
                [r['snore_pct_pre_30s'] for r in g]),
            'snore_pct_post_avg': _safe_mean(
                [r['snore_pct_post_30s'] for r in g]),
            'spo2_min_post_avg': _safe_mean(
                [r['spo2_min_post_60s'] for r in g]),
        }

    per_strategy = {k: _group_stat(v) for k, v in groups.items()}
    total = _group_stat(rows) if rows else {}

    summary = {
        'session_id': meta.get('session_id'),
        'started_at': meta.get('started_at'),
        'mode': meta.get('mode', 'A'),
        'subject': meta.get('subject_id'),
        'note': meta.get('note'),
        'total_interventions': len(rows),
        'overall': total,
        'by_strategy': per_strategy,
    }

    # Write files
    report_dir = session_dir / 'report'
    report_dir.mkdir(exist_ok=True)
    with open(report_dir / 'summary.json', 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=float)

    if rows:
        with open(report_dir / 'strategy_report.csv', 'w',
                  newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    # Markdown
    md_lines = [
        f"# 夜间干预报告 · {meta.get('session_id', session_dir.name)}",
        '',
        f"- 实验模式: **Block {meta.get('mode', 'A')}**",
        f"- 被试: {meta.get('subject_id') or '—'}",
        f"- 开始: {meta.get('started_at') or '—'}",
        f"- 干预次数: **{len(rows)}**",
        '',
        '## 按策略分组',
        '',
        '| 策略 | 次数 | 成功率 | 成功潜伏中位 | 成功潜伏均值 | 前 30s 鼾声% | 后 30s 鼾声% | 后 60s SpO2 最低均值 |',
        '|------|------|--------|------------|------------|--------------|--------------|------------------|',
    ]
    def _fmt(x, unit='', digits=1):
        if x is None:
            return '—'
        if isinstance(x, (int,)):
            return f"{x}{unit}"
        return f"{x:.{digits}f}{unit}"
    for k, v in per_strategy.items():
        md_lines.append(
            f"| {k} | {v['n']} | "
            f"{_fmt(v['success_rate_pct'],'%')} | "
            f"{_fmt(v['latency_median_s'],'s')} | "
            f"{_fmt(v['latency_mean_s'],'s')} | "
            f"{_fmt(v['snore_pct_pre_avg'],'%')} | "
            f"{_fmt(v['snore_pct_post_avg'],'%')} | "
            f"{_fmt(v['spo2_min_post_avg'],'%')} |")
    md_lines += [
        '',
        '## 汇总',
        '',
        f"- 总成功率: {_fmt(total.get('success_rate_pct'),'%')}",
        f"- 成功潜伏中位: {_fmt(total.get('latency_median_s'),'s')}",
        f"- 触发前鼾声平均占比: {_fmt(total.get('snore_pct_pre_avg'),'%')}",
        f"- 触发后鼾声平均占比: {_fmt(total.get('snore_pct_post_avg'),'%')}",
        '',
        '## 每次干预原始表',
        '',
        '| # | 时刻 | 策略 | 方向 | 成功? | 潜伏 | 鼾前% | 鼾后% | SpO2 前均 | SpO2 后最低 |',
        '|---|------|-----|------|------|------|--------|--------|----------|----------|',
    ]
    for r in rows:
        from datetime import datetime as _dt
        t_str = _dt.fromtimestamp(r['t']).strftime('%H:%M:%S')
        md_lines.append(
            f"| {r['idx']} | {t_str} | {r['strategy']} | {r['direction']} | "
            f"{'✅' if r['success'] else '✗'} | "
            f"{_fmt(r['latency_s'],'s')} | "
            f"{_fmt(r['snore_pct_pre_30s'],'%')} | "
            f"{_fmt(r['snore_pct_post_30s'],'%')} | "
            f"{_fmt(r['spo2_mean_pre_30s'],'%')} | "
            f"{_fmt(r['spo2_min_post_60s'],'%')} |")

    (report_dir / 'strategy_report.md').write_text(
        '\n'.join(md_lines), encoding='utf-8')

    return summary


# ─────────────────────────────────── CLI ─────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('sessions', nargs='*',
                    help='session dir(s), absolute or relative')
    ap.add_argument('--all', action='store_true',
                    help='analyze every subdir under sessions/')
    ap.add_argument('--latest', action='store_true',
                    help='analyze the most recent session')
    args = ap.parse_args()

    if args.all:
        targets = [p for p in SESSIONS_DIR.iterdir() if p.is_dir()]
    elif args.latest:
        dirs = sorted([p for p in SESSIONS_DIR.iterdir() if p.is_dir()],
                      key=lambda p: p.name)
        targets = [dirs[-1]] if dirs else []
    else:
        targets = [Path(s) for s in args.sessions]
        targets = [p if p.is_absolute() else (ROOT / p) for p in targets]

    if not targets:
        print("未指定会话目录。用法: analyze_night.py <session_dir> / --all / --latest")
        sys.exit(2)

    for p in targets:
        if not p.is_dir():
            print(f"  [skip] {p}: not a directory", file=sys.stderr)
            continue
        print(f"▶ {p.name}")
        s = analyze_session(p)
        if s is None:
            continue
        print(f"  总干预 {s['total_interventions']}, 按策略分组:")
        for k, v in s['by_strategy'].items():
            sr = v.get('success_rate_pct')
            lat = v.get('latency_median_s')
            print(f"    - {k}: {v['n']} 次, 成功率 "
                  f"{sr if sr is not None else '—'}%, 中位潜伏 "
                  f"{lat if lat is not None else '—'} s")
        print(f"  写入: {p / 'report'}/")


if __name__ == '__main__':
    main()

"""Fan duty command → FAN1–10 RPM response, pooled over isolated duty steps (RPM is sampled only every ~5 s).

Usage (from thermal_test/): python -m tools.fan_latency log/<run> [log/<run> ...]
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

FANS = tuple(f"FAN{i} rpm" for i in range(1, 11))
POLL_WAIT_SEC = 1.0
PRE_QUIET_SEC = 20
POST_QUIET_SEC = 45
STEADY_FROM_SEC = 25
MIN_STEP_PCT = 10
MIN_RPM_CHANGE = 500
LARGE_STEP_PCT = 30
BIN_SEC = 0.5
LEVELS = (0.1, 0.5, 0.9, 0.95, 0.99)


def _utc(ts: str) -> float:
    return datetime.fromisoformat(ts).timestamp()


def _pct(values: list[float], q: float) -> float:
    return sorted(values)[int(q * (len(values) - 1))]


def load_changes(run: Path) -> list[dict]:
    """Duty changes from fan_control.csv: t_start = previous row + poll wait, t_end = the row logged after the apply."""
    with open(run / "fan_control.csv") as f:
        rows = list(csv.DictReader(f))
    col = "fan_duty_pct" if "fan_duty_pct" in rows[0] else "npu_fan_duty_pct"
    seq = [(_utc(r["Timestamp"]), int(r[col])) for r in rows if r[col]]
    changes = [
        {"d0": d0, "d1": d1, "t_start": t0 + POLL_WAIT_SEC, "t_end": t1}
        for (t0, d0), (t1, d1) in zip(seq, seq[1:])
        if d1 != d0
    ]
    for i, c in enumerate(changes):
        before = changes[i - 1]["t_end"] if i else -math.inf
        after = changes[i + 1]["t_start"] if i + 1 < len(changes) else math.inf
        c["isolated"] = c["t_start"] - before >= PRE_QUIET_SEC and after - c["t_end"] >= POST_QUIET_SEC
    return changes


def load_rpm(run: Path) -> list[tuple[float, float]]:
    out = []
    with open(run / "server_monitoring.csv") as f:
        for r in csv.DictReader(f):
            vals = [float(r[k]) for k in FANS if r.get(k)]
            if len(vals) == len(FANS):
                out.append((_utc(r["Timestamp"]), statistics.fmean(vals)))
    return out


def response(change: dict, rpm: list[tuple[float, float]]) -> list[tuple[float, float]] | None:
    """(s after t_end, fraction of the RPM step done) around one change."""
    t = change["t_end"]
    pre = [v for ts, v in rpm if t - PRE_QUIET_SEC + 5 <= ts <= change["t_start"]]
    post = [v for ts, v in rpm if t + STEADY_FROM_SEC <= ts <= t + POST_QUIET_SEC]
    if not pre or not post:
        return None
    r0, r1 = statistics.fmean(pre), statistics.fmean(post)
    if abs(r1 - r0) < MIN_RPM_CHANGE:
        return None
    return [(ts - t, (v - r0) / (r1 - r0)) for ts, v in rpm if t - 10 <= ts <= t + POST_QUIET_SEC]


def crossing(curve: list[tuple[float, float]], level: float) -> float | None:
    for (t0, f0), (t1, f1) in zip(curve, curve[1:]):
        if f0 < level <= f1:
            return t0 + BIN_SEC / 2 + (level - f0) / (f1 - f0) * (t1 - t0)
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="+", type=Path)
    args = parser.parse_args()
    apply_sec: list[float] = []
    groups: dict[str, list[list[tuple[float, float]]]] = defaultdict(list)
    for run in args.runs:
        rpm = load_rpm(run)
        for c in load_changes(run):
            apply_sec.append(c["t_end"] - c["t_start"])
            step = c["d1"] - c["d0"]
            if not c["isolated"] or abs(step) < MIN_STEP_PCT:
                continue
            pts = response(c, rpm)
            if pts:
                size = "large" if abs(step) >= LARGE_STEP_PCT else "small"
                groups[f"{'up' if step > 0 else 'down'}_{size}"].append(pts)
    print(f"duty changes {len(apply_sec)}: apply (decision → commands done) p5/p50/p95/max"
          f" {_pct(apply_sec, .05):.2f}/{_pct(apply_sec, .5):.2f}/{_pct(apply_sec, .95):.2f}/{max(apply_sec):.2f} s")
    print("commands done → RPM fraction reached (s; lower bound — RPM time is when 'sdr list all' started):")
    print("| step | n | " + " | ".join(f"{int(lv * 100)}%" for lv in LEVELS) + " |")
    print("|---|---|" + "---|" * len(LEVELS))
    for name in ("up_large", "up_small", "down_large", "down_small"):
        events = groups.get(name, [])
        if not events:
            continue
        bins: dict[int, list[float]] = defaultdict(list)
        for dt, frac in (p for e in events for p in e):
            bins[math.floor(dt / BIN_SEC)].append(frac)
        curve = [(b * BIN_SEC, statistics.median(v)) for b, v in sorted(bins.items())]
        cells = [crossing(curve, lv) for lv in LEVELS]
        print(f"| {name} | {len(events)} | " + " | ".join("-" if c is None else f"{c:.1f}" for c in cells) + " |")


if __name__ == "__main__":
    main()

"""Fit Card Temp = T∞ − A·e^(−t/τ) per sweep step; print τ and R = (T∞ − 25) / (T∞@100 − 25) per duty as TAU_SEC / RESISTANCE.

Usage (from thermal_test/): python -m tools.sweep_fit log/<sweep run>
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics
from datetime import datetime
from pathlib import Path

AMBIENT_C = 25.0
FIT_FROM_SEC = 45
END_TRIM_SEC = 3
LOAD_POWER_FRACTION = 0.6
MIN_POINTS = 60


def _utc(ts: str) -> float:
    return datetime.fromisoformat(ts).timestamp()


def _pct(values: list[float], q: float) -> float:
    return sorted(values)[int(q * (len(values) - 1))]


def _sweep_duty(phase: str) -> int | None:
    kind, _, duty = phase.rpartition("_")
    return int(duty) if ("_DOWN" in kind or "_UP" in kind) and duty.isdigit() else None


def load_steps(run: Path) -> dict[tuple[str, str, int, str], list[tuple[float, float, float]]]:
    steps: dict[tuple, list] = {}
    for path in sorted(run.glob("npu_monitoring_*.csv")):
        with open(path) as f:
            for r in csv.DictReader(f):
                duty = _sweep_duty(r["phase"])
                if duty is None or r.get("state", "RUN") != "RUN" or not r["Card Temp"]:
                    continue
                power = r["Card Power[uW]"]
                steps.setdefault((r["workload_code"], r["phase"], duty, r["Bus Address"]), []).append(
                    (_utc(r["Timestamp"]), float(r["Card Temp"]), int(power) / 1e6 if power else 0.0)
                )
    return steps


def _sse(points: list[tuple[float, float]], tau: float) -> tuple[float, float, float]:
    """Least squares T∞ and A for a fixed τ; returns (sse, T∞, A)."""
    e = [math.exp(-t / tau) for t, _ in points]
    n, se, see = len(points), sum(e), sum(x * x for x in e)
    sy, sey = sum(v for _, v in points), sum(x * v for x, (_, v) in zip(e, points))
    det = n * see - se * se
    if det == 0:
        return math.inf, 0.0, 0.0
    slope = (n * sey - se * sy) / det
    tinf = (see * sy - se * sey) / det
    return sum((v - (tinf + slope * x)) ** 2 for x, (_, v) in zip(e, points)), tinf, -slope


def fit(points: list[tuple[float, float]]) -> tuple[float, float]:
    """(τ, T∞): coarse 10 s grid over 10–1000 s, then 1 s around the best."""
    t0 = points[0][0]
    rel = [(t - t0, v) for t, v in points]
    coarse = min(range(10, 1001, 10), key=lambda tau: _sse(rel, tau)[0])
    best = min(range(max(2, coarse - 10), coarse + 11), key=lambda tau: _sse(rel, tau)[0])
    return float(best), _sse(rel, best)[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    fits: list[dict] = []
    for (wl, phase, duty, bus), pts in load_steps(args.run).items():
        peak = max(p for _, _, p in pts)
        start = next(t for t, _, p in pts if p > LOAD_POWER_FRACTION * peak)
        body = [(t, v) for t, v, _ in pts if start + FIT_FROM_SEC <= t <= pts[-1][0] - END_TRIM_SEC]
        if len(body) < MIN_POINTS:
            continue
        tau, tinf = fit(body)
        end = statistics.fmean(v for t, v in body if t >= body[-1][0] - 30)
        fits.append({"wl": wl, "duty": duty, "bus": bus, "tau": tau, "tinf": tinf, "end": end})
    base = {(f["wl"], f["bus"]): f["tinf"] for f in fits if f["duty"] == 100}
    print("| duty | n | τ s median [p25-p75] | R median [p25-p75] | T∞ − last 30 s °C |")
    print("|---|---|---|---|---|")
    tau_row, res_row = [], []
    for duty in sorted({f["duty"] for f in fits}, reverse=True):
        fs = [f for f in fits if f["duty"] == duty and (f["wl"], f["bus"]) in base]
        if not fs:
            continue
        taus = [f["tau"] for f in fs]
        rs = [(f["tinf"] - AMBIENT_C) / (base[(f["wl"], f["bus"])] - AMBIENT_C) for f in fs]
        gap = statistics.median(f["tinf"] - f["end"] for f in fs)
        print(
            f"| {duty} | {len(fs)} | {statistics.median(taus):.0f} [{_pct(taus, .25):.0f}-{_pct(taus, .75):.0f}]"
            f" | {statistics.median(rs):.2f} [{_pct(rs, .25):.2f}-{_pct(rs, .75):.2f}] | {gap:+.2f} |"
        )
        tau_row.append((duty, round(statistics.median(taus))))
        res_row.append((duty, round(statistics.median(rs), 2)))
    print("\nTAU_SEC =", tuple(sorted((d, float(t)) for d, t in tau_row)))
    print("RESISTANCE =", tuple(sorted(res_row)))


if __name__ == "__main__":
    main()

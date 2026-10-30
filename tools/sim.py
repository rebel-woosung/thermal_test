"""Closed-loop TARGET sim: group Card Temp of 8 cards on a plant fit to the CR03 duty sweep and fan latency data.

Usage (from thermal_test/): python -m tools.sim pred_lin_to_exp pred_exp_tau2_k2_h45 [--apply-sec 2.0] [--tau-scale 1.2]
"""

from __future__ import annotations

import argparse
import math
import random
import statistics
from itertools import pairwise

from fan_control.constants import FAN_MAX_DUTY, FAN_MIN_DUTY
from fan_control.target_control import TargetControl, create

FAN_TARGET_RISE_C = 10
TARGET_PHASE_SEC = 480
AMBIENT_C = 25.0
IDLE_C = 40.0
FAST_FRACTION = 0.55
FAST_TAU_SEC = 3.0
CARD_OFFSET_SD_C = 1.0
CARD_NOISE_SD_C = 0.5
TAU_SEC: tuple[tuple[int, float], ...] = (
    (20, 115.0), (30, 83.0), (40, 59.0), (50, 48.0), (60, 38.0),
    (70, 36.0), (80, 30.0), (90, 28.0), (100, 26.0),
)
RESISTANCE: tuple[tuple[int, float], ...] = (
    (20, 2.38), (30, 1.93), (40, 1.48), (50, 1.28), (60, 1.16),
    (70, 1.09), (80, 1.04), (90, 1.01), (100, 1.0),
)
BASELINES_C = (45.0, 52.0, 58.0, 63.0)
LOAD_DELAYS_SEC = (2.0, 50.0)
SEEDS = range(3)
STEP_SEC = 0.1
REACH_C = 0.5


def _interp(table: tuple[tuple[int, float], ...], x: float) -> float:
    if x <= table[0][0]:
        return table[0][1]
    for (x0, y0), (x1, y1) in pairwise(table):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return table[-1][1]


def run(
    law: str | type[TargetControl],
    baseline_c: float,
    load_delay_sec: float = 2.0,
    apply_sec: float = 2.0,
    fan_delay_sec: float = 4.0,
    tau_scale: float = 1.0,
    seed: int = 0,
) -> dict:
    """One TARGET phase: load at ``load_delay_sec``, a duty change is felt ``apply_sec + fan_delay_sec`` later."""
    rng = random.Random(seed)
    offsets = [rng.gauss(0, CARD_OFFSET_SD_C) for _ in range(8)]
    fast_c = FAST_FRACTION * (baseline_c - IDLE_C)
    target = baseline_c + FAN_TARGET_RISE_C
    ctl = create(law) if isinstance(law, str) else law()
    wanted = applied = FAN_MAX_DUTY
    effective = float(FAN_MAX_DUTY)
    heatsink, fast, t, next_poll = IDLE_C, 0.0, 0.0, 0.0
    pending: list[tuple[float, float]] = []
    samples: list[tuple[float, float, float, int]] = []
    changes: list[float] = []
    ctl.reset(t, wanted)
    while t < TARGET_PHASE_SEC:
        while pending and pending[0][0] <= t:
            effective = pending.pop(0)[1]
        if t >= load_delay_sec:
            steady = AMBIENT_C + (baseline_c - AMBIENT_C) * _interp(RESISTANCE, effective)
            heatsink_target = steady - fast_c
            fast += (fast_c - fast) * (1 - math.exp(-STEP_SEC / FAST_TAU_SEC))
        else:
            heatsink_target = IDLE_C
        tau = _interp(TAU_SEC, effective) * tau_scale
        heatsink += (heatsink_target - heatsink) * (1 - math.exp(-STEP_SEC / tau))
        card = heatsink + fast
        if t >= next_poll:
            temps = sorted(round(card + o + rng.gauss(0, CARD_NOISE_SD_C)) for o in offsets)
            group = sum(temps[1:-1]) / (len(temps) - 2)
            wanted = max(FAN_MIN_DUTY, min(FAN_MAX_DUTY, ctl.update(t, {"group": group}, {"group": target}, wanted)))
            busy = 0.0
            if wanted != applied:
                busy = apply_sec
                pending.append((t + apply_sec + fan_delay_sec, float(wanted)))
                applied = wanted
                changes.append(t)
            samples.append((t, card, group, applied))
            next_poll = t + 1.0 + busy
        t += STEP_SEC
    return _evaluate(samples, changes, target, load_delay_sec)


def _evaluate(samples: list[tuple[float, float, float, int]], changes: list[float], target: float, load_delay_sec: float) -> dict:
    times = [s[0] for s in samples]
    groups = [s[2] for s in samples]
    smooth = [statistics.fmean(groups[max(0, i - 5) : i + 6]) for i in range(len(groups))]
    reach = next((i for i, v in enumerate(smooth) if v >= target - REACH_C), None)
    if reach is None:
        return {"reach_s": None}
    post_len = times[-1] - times[reach]
    cards = [s[1] for s in samples[reach:]]
    return {
        "reach_s": times[reach] - load_delay_sec,
        "overshoot_c": max(0.0, max(smooth[reach:]) - target),
        "sd_c": statistics.pstdev(cards),
        "mean_err_c": statistics.fmean(cards) - target,
        "changes_per5min": sum(1 for c in changes if c >= times[reach]) / post_len * 300 if post_len > 0 else 0.0,
        "duty_end": samples[-1][3],
    }


def summarize(name: str, law: str | type[TargetControl], **kwargs: float) -> dict | None:
    rs = [
        run(law, b, load_delay_sec=d, seed=s, **kwargs)
        for b in BASELINES_C
        for d in LOAD_DELAYS_SEC
        for s in SEEDS
    ]
    ok = [r for r in rs if r["reach_s"] is not None]
    if not ok:
        print(f"{name:28s} never reached")
        return None
    out = {k: statistics.fmean(r[k] for r in ok) for k in ("reach_s", "overshoot_c", "sd_c", "mean_err_c", "changes_per5min")}
    print(
        f"{name:28s} n={len(ok)}/{len(rs)} reach {out['reach_s']:5.0f} s"
        f" [{min(r['reach_s'] for r in ok):.0f}-{max(r['reach_s'] for r in ok):.0f}]"
        f"  OS {out['overshoot_c']:.2f}/{max(r['overshoot_c'] for r in ok):.2f} °C"
        f"  sd {out['sd_c']:.2f}  mean err {out['mean_err_c']:+.2f}  chg/5min {out['changes_per5min']:.1f}"
    )
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("laws", nargs="+")
    parser.add_argument("--apply-sec", type=float, default=2.0, help="fan command time (3.8 with CPU zones every apply)")
    parser.add_argument("--fan-delay-sec", type=float, default=4.0, help="command done → RPM moved")
    parser.add_argument("--tau-scale", type=float, default=1.0, help="plant TAU_SEC multiplier (robustness check)")
    args = parser.parse_args()
    for law in args.laws:
        summarize(law, law, apply_sec=args.apply_sec, fan_delay_sec=args.fan_delay_sec, tau_scale=args.tau_scale)


if __name__ == "__main__":
    main()

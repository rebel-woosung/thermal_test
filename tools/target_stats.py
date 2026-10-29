"""TARGET-phase stats per law: reach time, overshoot, post-reach 8-card error, fan duty changes.

Usage (from thermal_test/): python -m tools.target_stats log/<run> [log/<run> ...] [--exclude RE0MR0013] [--csv out.csv]
"""

from __future__ import annotations

import argparse
import ast
import csv
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

REACH_C = 0.5
EVAL_SEC = 400
END_TRIM_SEC = 3
SMOOTH_HALF_SEC = 5
SETTLE_BAND_C = 1.0

_PER_CARD = re.compile(
    r"^(\S+ \S+) \[INFO\] \[FanController\] TARGET phase — Card Temp target \(baseline \+ (\d+)°C\) by bus: (\{.*\})"
)
_GROUP = re.compile(
    r"^(\S+ \S+) \[INFO\] \[FanController\] TARGET phase — group Card Temp target \(baseline \+ (\d+)°C\):"
    r" ([\d.]+)°C, card baselines: (\{.*\})"
)
_LOG_TIME = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3})")


def _utc(ts: str) -> float:
    return datetime.fromisoformat(ts).timestamp()


def _trimmed(values: list[float]) -> float:
    v = sorted(values)
    kept = v[1:-1] if len(v) > 2 else v
    return sum(kept) / len(kept)


def _pct(values: list[float], q: float) -> float:
    v = sorted(values)
    return v[min(len(v) - 1, max(0, int(q * (len(v) - 1))))]


def _centered(values: list[float | None]) -> list[float | None]:
    out: list[float | None] = []
    for i, value in enumerate(values):
        window = [v for v in values[max(0, i - SMOOTH_HALF_SEC) : i + SMOOTH_HALF_SEC + 1] if v is not None]
        out.append(statistics.fmean(window) if window and value is not None else None)
    return out


def _log_offset(run: Path) -> timedelta:
    """server.log local time − UTC, from its first line vs the first fan_control.csv row (rounded to 15 min)."""
    with open(run / "server.log", errors="replace") as f:
        first = _LOG_TIME.match(f.readline())
    with open(run / "fan_control.csv") as f:
        row = next(csv.DictReader(f))
    if not first:
        return timedelta(hours=9)
    local = datetime.strptime(first.group(1), "%Y-%m-%d %H:%M:%S,%f")
    utc = datetime.fromisoformat(row["Timestamp"]).astimezone(timezone.utc).replace(tzinfo=None)
    quarters = round((local - utc).total_seconds() / 900)
    return timedelta(seconds=quarters * 900)


def _log_utc(ts: str, offset: timedelta) -> float:
    return (datetime.strptime(ts, "%Y-%m-%d %H:%M:%S,%f") - offset).replace(tzinfo=timezone.utc).timestamp()


def load_targets(run: Path) -> list[tuple[float, dict]]:
    offset = _log_offset(run)
    out = []
    for line in (run / "server.log").read_text(errors="replace").splitlines():
        if m := _GROUP.match(line):
            rise = float(m.group(2))
            baselines = ast.literal_eval(m.group(4))
            cards = {bus: v + rise for bus, v in baselines.items()}
            out.append((_log_utc(m.group(1), offset), {"group": float(m.group(3)), "cards": cards}))
        elif m := _PER_CARD.match(line):
            out.append((_log_utc(m.group(1), offset), {"group": None, "cards": ast.literal_eval(m.group(3))}))
    return out


def load_segments(run: Path) -> list[dict]:
    segs: dict[tuple, dict] = {}
    with open(run / "fan_control.csv") as f:
        for r in csv.DictReader(f):
            if r["fan_mode"] != "TARGET" or r["workload_code"] in ("REST", "HW_CFG") or "_TARGET_" not in r["phase"]:
                continue
            if r.get("state", "RUN") != "RUN":
                continue
            key = (r["Stage Type"], r["workload_code"], r["phase"], r["iteration_index"])
            duty = r["fan_duty_pct"]
            segs.setdefault(key, []).append((_utc(r["Timestamp"]), int(duty) if duty else None))
    return sorted(
        (
            {"key": key, "law": key[2].split("_TARGET_", 1)[1], "workload": key[1], "rows": rows, "t0": rows[0][0], "t1": rows[-1][0]}
            for key, rows in segs.items()
        ),
        key=lambda s: s["t0"],
    )


def load_cards(run: Path) -> dict[str, list[tuple[float, str, str, float, float | None]]]:
    cards: dict[str, list] = {}
    for path in sorted(run.glob("npu_monitoring_*.csv")):
        with open(path) as f:
            for r in csv.DictReader(f):
                temp = r["Card Temp"]
                if "TARGET" not in r["phase"] or not temp or float(temp) <= 0:
                    continue
                power = r["Card Power[uW]"]
                cards.setdefault(r["Bus Address"], []).append(
                    (_utc(r["Timestamp"]), r["workload_code"], r["phase"], float(temp), int(power) / 1e6 if power else None)
                )
    return cards


def _grid(cards: dict, buses: list[str], seg: dict) -> tuple[dict[str, list], dict[str, list]]:
    n = int(seg["t1"] - seg["t0"])
    temp: dict[str, list] = {}
    power: dict[str, list] = {}
    for bus in buses:
        tc: list = [None] * (n + 1)
        pc: list = [None] * (n + 1)
        for ts, wl, phase, t, p in cards.get(bus, []):
            if wl == seg["workload"] and phase == seg["key"][2] and seg["t0"] <= ts <= seg["t1"]:
                i = int(ts - seg["t0"])
                tc[i], pc[i] = t, p
        for col in (tc, pc):
            last = None
            for i in range(n + 1):
                if col[i] is None:
                    col[i] = last
                else:
                    last = col[i]
        temp[bus], power[bus] = tc, pc
    return temp, power


def _load_window(power: dict[str, list]) -> tuple[int, int] | None:
    """First and last second the trimmed-mean Card Power is above the midpoint of its 5th/95th percentile."""
    n = len(next(iter(power.values())))
    p = []
    for i in range(n):
        vals = [c[i] for c in power.values() if c[i] is not None]
        p.append(_trimmed(vals) if len(vals) == len(power) else None)
    valid = [v for v in p if v is not None]
    if not valid:
        return None
    lo, hi = _pct(valid, 0.05), _pct(valid, 0.95)
    thr = (lo + hi) / 2 if hi - lo >= 40 else -1
    idx = [i for i, v in enumerate(p) if v is not None and v > thr]
    return (idx[0], idx[-1]) if idx else None


def analyze(seg: dict, target: dict, cards: dict) -> dict:
    buses = sorted(target["cards"])
    res = {"law": seg["law"], "workload": seg["workload"], "stage": seg["key"][0]}
    temp, power = _grid(cards, buses, seg)
    win = _load_window(power)
    if win is None:
        return {**res, "error": "no load window"}
    start, stop = win
    end = min(stop - END_TRIM_SEC, start + EVAL_SEC)
    is_group = target["group"] is not None
    group_target = target["group"] if is_group else _trimmed(list(target["cards"].values()))
    cv: list[float | None] = []
    worst: list[float | None] = []
    errs: list[dict[str, float] | None] = []
    for i in range(start, end + 1):
        row = {b: temp[b][i] for b in buses if temp[b][i] is not None}
        if len(row) < len(buses):
            cv.append(None)
            worst.append(None)
            errs.append(None)
            continue
        e = {b: v - target["cards"][b] for b, v in row.items()}
        worst.append(max(e.values()))
        cv.append(_trimmed(list(row.values())) - group_target if is_group else max(e.values()))
        errs.append(e)
    cv_s, worst_s = _centered(cv), _centered(worst)
    reach = next((i for i, c in enumerate(cv_s) if c is not None and c >= -REACH_C), None)
    duty_rows = [(ts - seg["t0"] - start, d) for ts, d in seg["rows"] if d is not None and ts - seg["t0"] <= end]
    res.update({"control": "group" if is_group else "hottest", "load_delay_s": start, "reach_s": reach})
    if reach is None:
        return res
    post_cv = [c for c in cv_s[reach:] if c is not None]
    post_worst = [c for c in worst_s[reach:] if c is not None]
    pooled = [v for e in errs[reach:] if e for v in e.values()]
    per_card = {b: [e[b] for e in errs[reach:] if e] for b in buses}
    prev = next((d for t, d in reversed(duty_rows) if t < reach), None)
    changes = 0
    for t, d in duty_rows:
        if t < reach:
            continue
        if prev is not None and d != prev:
            changes += 1
        prev = d
    post_len = len(cv) - 1 - reach
    unsettled = [i for i, v in enumerate(post_cv) if abs(v) > SETTLE_BAND_C]
    res.update(
        {
            "overshoot_c": max(0.0, max(post_cv)),
            "hottest_overshoot_c": max(0.0, max(post_worst)),
            "settle_s": reach + (unsettled[-1] + 1 if unsettled else 0),
            "e_max": max(pooled),
            "e_min": min(pooled),
            "e_p01": _pct(pooled, 0.01),
            "e_p99": _pct(pooled, 0.99),
            "e_mean": statistics.fmean(pooled),
            "e_sd": statistics.pstdev(pooled),
            "e_time_sd": statistics.fmean(statistics.pstdev(v) for v in per_card.values()),
            "e_card_sd": statistics.pstdev([statistics.fmean(v) for v in per_card.values()]),
            "changes": changes,
            "changes_per5min": changes / post_len * 300 if post_len > 0 else 0.0,
            "duty_end": duty_rows[-1][1] if duty_rows else None,
            "pooled": pooled,
        }
    )
    return res


def table(results: list[dict]) -> None:
    laws = list(dict.fromkeys(r["law"] for r in results))
    print("| law | ctrl | n | reach s avg [min-max] | settle s | OS °C avg/max | hottest OS avg/max | e max/min | e P1/P99"
          " | e μ ± σ | σ time/card | chg/5min (n) |")
    print("|" + "---|" * 12)
    for law in laws:
        ss = [r for r in results if r["law"] == law and r.get("reach_s") is not None]
        missed = sum(1 for r in results if r["law"] == law and r.get("reach_s") is None)
        if not ss:
            print(f"| {law} | | 0 (+{missed} not reached) |")
            continue
        pooled = [v for r in ss for v in r["pooled"]]
        mean = {
            k: statistics.fmean(r[k] for r in ss)
            for k in ("reach_s", "settle_s", "overshoot_c", "hottest_overshoot_c", "e_time_sd", "e_card_sd",
                      "changes_per5min", "changes")
        }
        peak = {k: max(r[k] for r in ss) for k in ("overshoot_c", "hottest_overshoot_c")}
        reach = [r["reach_s"] for r in ss]
        print(
            f"| {law} | {ss[0]['control']} | {len(ss)}{f' (+{missed} not reached)' if missed else ''}"
            f" | {mean['reach_s']:.0f} [{min(reach)}-{max(reach)}] | {mean['settle_s']:.0f}"
            f" | {mean['overshoot_c']:.1f}/{peak['overshoot_c']:.1f}"
            f" | {mean['hottest_overshoot_c']:.1f}/{peak['hottest_overshoot_c']:.1f}"
            f" | {max(pooled):+.1f}/{min(pooled):+.1f} | {_pct(pooled, 0.01):+.1f}/{_pct(pooled, 0.99):+.1f}"
            f" | {statistics.fmean(pooled):+.2f} ± {statistics.pstdev(pooled):.2f}"
            f" | {mean['e_time_sd']:.2f}/{mean['e_card_sd']:.2f}"
            f" | {mean['changes_per5min']:.1f} ({mean['changes']:.1f}) |"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--exclude", nargs="*", default=[], help="workload codes left out of the table")
    parser.add_argument("--csv", type=Path, help="write per-segment rows here")
    args = parser.parse_args()
    results = []
    for run in args.runs:
        targets = load_targets(run)
        cards = load_cards(run)
        for seg in load_segments(run):
            prior = [t for ts, t in targets if ts <= seg["t0"]]
            if not prior:
                print(f"skip {run.name} {seg['key']}: no TARGET target logged before it")
                continue
            results.append({"run": run.name, **analyze(seg, prior[-1], cards)})
    shown = [r for r in results if r["workload"] not in args.exclude and "error" not in r]
    print(f"{len(shown)} segments from {len(args.runs)} run(s); evaluated {EVAL_SEC} s from load start")
    table(shown)
    if args.csv:
        keys = [k for k in dict.fromkeys(k for r in results for k in r) if k != "pooled"]
        with open(args.csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(results)
        print(f"per-segment rows -> {args.csv}")


if __name__ == "__main__":
    main()

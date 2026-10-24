"""Fan controller — NPU fan zones at max, except the latter part of each workload, where
the duty is stepped so the hottest-rising card sits FAN_TARGET_RISE_C above its baseline.

Per workload (test_time T):
  0 ~ T·FAN_MAX_PHASE_RATIO  NPU zones at FAN_MAX_DUTY.
  rest of T                  target = each card's max Card Temp over the last
                             FAN_BASELINE_WINDOW_SEC + FAN_TARGET_RISE_C. Every
                             FAN_CONTROL_INTERVAL_SEC the duty moves by
                             max(card temp − target) × FAN_GAIN_PCT_PER_C, capped at
                             ±FAN_MAX_STEP_PCT, unchanged within ±FAN_DEADBAND_C,
                             kept within FAN_MIN_DUTY..FAN_MAX_DUTY.
HW_CFG and REST run at FAN_MAX_DUTY; CPU zones stay at FAN_MAX_DUTY all session.
Card Temps come from the NPU collectors (no separate poll).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from pathlib import Path

import commands
from common import BaseWorker, CardTempRegistry, CurrentRowTag, append_row, ensure_header, now_iso
from constants import (
    FAN_BASELINE_WINDOW_SEC,
    FAN_CONTROL_INTERVAL_SEC,
    FAN_CPU_ZONES,
    FAN_DEADBAND_C,
    FAN_GAIN_PCT_PER_C,
    FAN_MAX_DUTY,
    FAN_MAX_PHASE_RATIO,
    FAN_MAX_STEP_PCT,
    FAN_MIN_DUTY,
    FAN_NPU_ZONES,
    FAN_POLL_INTERVAL_SEC,
    FAN_TARGET_RISE_C,
)

logger = logging.getLogger(__name__)

_MAX_MODE = "MAX"
_RISE_MODE = "RISE"

_FAN_HEADER: tuple[str, ...] = (
    "Timestamp",
    "Stage Type",
    "workload_code",
    "iteration_index",
    "fan_mode",
    "max_card_temp",
    "max_over_target_c",
    "npu_fan_duty_pct",
)
_FAN_KEYS: tuple[str, ...] = (
    "timestamp",
    "stage_type",
    "workload_code",
    "iteration_index",
    "mode",
    "max_temp",
    "over_target",
    "duty",
)


class FanController(BaseWorker):
    def __init__(
        self,
        card_temps: CardTempRegistry,
        current_tag: CurrentRowTag,
        out_dir: Path,
        stop_event: threading.Event,
    ) -> None:
        super().__init__("fan-controller", stop_event)
        self._card_temps = card_temps
        self._current_tag = current_tag
        self._csv_path = out_dir / "fan_control.csv"
        self._lock = threading.Lock()
        self._rise_at: float | None = None
        self._targets: dict[int, float] | None = None
        self._history: deque[tuple[float, dict[int, float]]] = deque()
        self._next_control = 0.0
        self._wanted_duty = FAN_MAX_DUTY
        self._applied_duty: int | None = None
        ensure_header(self._csv_path, _FAN_HEADER)

    def hold_max(self) -> None:
        with self._lock:
            self._rise_at = None
            self._targets = None

    def begin_workload(self, test_time_sec: float) -> None:
        with self._lock:
            self._rise_at = time.monotonic() + test_time_sec * FAN_MAX_PHASE_RATIO
            self._targets = None

    def run(self, stop_event: threading.Event) -> None:
        logger.info(
            "[FanController] started — NPU zones %s controlled, CPU zones %s at %d%%",
            [hex(z) for z in FAN_NPU_ZONES],
            [hex(z) for z in FAN_CPU_ZONES],
            FAN_MAX_DUTY,
        )
        while not stop_event.is_set():
            now = time.monotonic()
            temps = self._card_temps.snapshot()
            self._remember(now, temps)
            with self._lock:
                mode, over_target = self._update_wanted_duty(now, temps)
            if self._wanted_duty != self._applied_duty:
                self._apply(self._wanted_duty)
            self._log_row(mode, temps, over_target)
            if stop_event.wait(FAN_POLL_INTERVAL_SEC):
                break

    def _remember(self, now: float, temps: dict[int, float]) -> None:
        self._history.append((now, temps))
        while now - self._history[0][0] > FAN_BASELINE_WINDOW_SEC:
            self._history.popleft()

    def _update_wanted_duty(self, now: float, temps: dict[int, float]) -> tuple[str, float | None]:
        if self._rise_at is None or now < self._rise_at:
            self._wanted_duty = FAN_MAX_DUTY
            return _MAX_MODE, None
        if self._targets is None:
            self._targets = self._baseline_targets()
            self._next_control = now
        over = [temps[slot] - target for slot, target in self._targets.items() if slot in temps]
        if not over:
            return _RISE_MODE, None
        worst = max(over)
        if now >= self._next_control:
            self._next_control = now + FAN_CONTROL_INTERVAL_SEC
            self._wanted_duty = _next_duty(self._wanted_duty, worst)
        return _RISE_MODE, worst

    def _baseline_targets(self) -> dict[int, float]:
        samples: dict[int, list[float]] = {}
        for _, temps in self._history:
            for slot, temp in temps.items():
                samples.setdefault(slot, []).append(temp)
        targets = {slot: max(v) + FAN_TARGET_RISE_C for slot, v in samples.items()}
        logger.info(
            "[FanController] rise phase — Card Temp target (baseline + %d°C) by slot: %s",
            FAN_TARGET_RISE_C,
            {slot: round(t, 1) for slot, t in sorted(targets.items())},
        )
        return targets

    def _apply(self, duty: int) -> None:
        try:
            commands.set_fan_duty_all(FAN_MAX_DUTY, FAN_CPU_ZONES)
            commands.set_fan_duty_all(duty, FAN_NPU_ZONES)
            logger.info("[FanController] NPU fan duty -> %d%%", duty)
            self._applied_duty = duty
        except Exception as e:
            logger.error("[FanController] set NPU duty %d%% failed: %s", duty, e)

    def _log_row(self, mode: str, temps: dict[int, float], over_target: float | None) -> None:
        tag = self._current_tag.tag
        append_row(
            self._csv_path,
            _FAN_KEYS,
            {
                "timestamp": now_iso(),
                "stage_type": tag.stage_type,
                "workload_code": tag.workload_code,
                "iteration_index": tag.iteration_index,
                "mode": mode,
                "max_temp": f"{max(temps.values()):.1f}" if temps else "",
                "over_target": "" if over_target is None else f"{over_target:+.1f}",
                "duty": "" if self._applied_duty is None else self._applied_duty,
            },
        )


def _next_duty(duty: int, over_target_c: float) -> int:
    """Step the duty up while the hottest-rising card is above its target, down while below."""
    if abs(over_target_c) <= FAN_DEADBAND_C:
        return duty
    step = max(-FAN_MAX_STEP_PCT, min(FAN_MAX_STEP_PCT, round(over_target_c * FAN_GAIN_PCT_PER_C)))
    return max(FAN_MIN_DUTY, min(FAN_MAX_DUTY, duty + step))

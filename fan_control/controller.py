"""Fan controller — NPU fan zone duty per workload phase, switched by main.py:

  MAX     FAN_MAX_DUTY.
  TARGET  group Card Temp = mean of the cards without the hottest and coldest one; target = its
          mean over the last FAN_BASELINE_WINDOW_SEC of the MAX phase (``capture_baseline``)
          + FAN_TARGET_RISE_C; the law named in ``start_target`` (target_control/<name>.py)
          steers the group Card Temp to it, one per FAN_TARGET_CONTROLS.
  FIXED   the given duty; GUARD (FAN_MAX_DUTY) for the rest of the step once any Card or
          HBM temp reaches SWEEP_SAFETY_TEMP_C.
HW_CFG and REST run at FAN_MAX_DUTY; CPU zones stay at FAN_MAX_DUTY all session.
Card/HBM temps come from the NPU collectors (no separate poll).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from pathlib import Path

import commands
from common import BaseWorker, CardTempRegistry, CurrentRowTag, append_row, ensure_header, now_iso
from fan_control import target_control
from fan_control.constants import (
    FAN_BASELINE_WINDOW_SEC,
    FAN_CPU_ZONES,
    FAN_MAX_DUTY,
    FAN_MIN_DUTY,
    FAN_NPU_ZONES,
    FAN_POLL_INTERVAL_SEC,
)
from test_config import FAN_TARGET_RISE_C, SWEEP_SAFETY_TEMP_C

logger = logging.getLogger(__name__)

_MAX_MODE = "MAX"
_TARGET_MODE = "TARGET"
_FIXED_MODE = "FIXED"
_GUARD_MODE = "GUARD"
_GROUP = "group"

_FAN_HEADER: tuple[str, ...] = (
    "Timestamp",
    "Stage Type",
    "workload_code",
    "phase",
    "state",
    "iteration_index",
    "fan_mode",
    "max_card_temp",
    "max_over_target_c",
    "fan_duty_pct",
    "group_card_temp",
    "group_over_target_c",
)
_FAN_KEYS: tuple[str, ...] = (
    "timestamp",
    "stage_type",
    "workload_code",
    "phase",
    "state",
    "iteration_index",
    "mode",
    "max_temp",
    "over_target",
    "duty",
    "group_temp",
    "group_over_target",
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
        self._mode = _MAX_MODE
        self._fixed_duty = FAN_MAX_DUTY
        self._target: float | None = None
        self._history: deque[tuple[float, dict[str, float]]] = deque()
        self._target_control: target_control.TargetControl | None = None
        self._wanted_duty = FAN_MAX_DUTY
        self._applied_duty: int | None = None
        ensure_header(self._csv_path, _FAN_HEADER)

    def hold_max(self) -> None:
        with self._lock:
            self._mode = _MAX_MODE

    def hold_duty(self, duty: int) -> None:
        with self._lock:
            self._mode = _FIXED_MODE
            self._fixed_duty = duty

    def capture_baseline(self) -> None:
        """Fix the TARGET-phase target from the group Card Temp over the last
        FAN_BASELINE_WINDOW_SEC — call at the end of the MAX phase."""
        with self._lock:
            self._target = self._baseline_target()

    def start_target(self, control: str) -> None:
        with self._lock:
            if self._target is None:
                logger.warning("[FanController] no MAX-phase baseline — TARGET phase stays at max")
            logger.info("[FanController] TARGET phase — control %s", control)
            self._mode = _TARGET_MODE
            self._wanted_duty = FAN_MAX_DUTY
            self._target_control = target_control.create(control)
            self._target_control.reset(time.monotonic(), self._wanted_duty)

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
            hottest = self._card_temps.hottest()
            with self._lock:
                self._remember(now, temps)
                mode = self._update_wanted_duty(now, temps, hottest)
                target = self._target if mode == _TARGET_MODE else None
            if self._wanted_duty != self._applied_duty:
                self._apply(self._wanted_duty)
            self._log_row(mode, temps, target)
            if stop_event.wait(FAN_POLL_INTERVAL_SEC):
                break

    def _remember(self, now: float, temps: dict[str, float]) -> None:
        self._history.append((now, temps))
        while now - self._history[0][0] > FAN_BASELINE_WINDOW_SEC:
            self._history.popleft()

    def _update_wanted_duty(self, now: float, temps: dict[str, float], hottest: float | None) -> str:
        if self._mode == _TARGET_MODE:
            self._step_toward_target(now, temps)
            return _TARGET_MODE
        if self._mode == _FIXED_MODE and _over_safety_temp(hottest):
            logger.warning(
                "[FanController] %.0f°C ≥ SWEEP_SAFETY_TEMP_C %s — %d%% step held at %d%%",
                hottest,
                SWEEP_SAFETY_TEMP_C,
                self._fixed_duty,
                FAN_MAX_DUTY,
            )
            self._mode = _GUARD_MODE
        self._wanted_duty = self._fixed_duty if self._mode == _FIXED_MODE else FAN_MAX_DUTY
        return self._mode

    def _step_toward_target(self, now: float, temps: dict[str, float]) -> None:
        group = _group_temp(temps)
        if group is None or self._target is None or self._target_control is None:
            return
        duty = self._target_control.update(now, {_GROUP: group}, {_GROUP: self._target}, self._wanted_duty)
        self._wanted_duty = max(FAN_MIN_DUTY, min(FAN_MAX_DUTY, duty))

    def _baseline_target(self) -> float | None:
        groups = [group for _, temps in self._history if (group := _group_temp(temps)) is not None]
        if not groups:
            return None
        target = sum(groups) / len(groups) + FAN_TARGET_RISE_C
        samples: dict[str, list[float]] = {}
        for _, temps in self._history:
            for bus, temp in temps.items():
                samples.setdefault(bus, []).append(temp)
        logger.info(
            "[FanController] TARGET phase — group Card Temp target (baseline + %d°C): %.1f°C,"
            " card baselines: %s",
            FAN_TARGET_RISE_C,
            target,
            {bus: round(sum(v) / len(v), 1) for bus, v in sorted(samples.items())},
        )
        return target

    def _apply(self, duty: int) -> None:
        try:
            commands.set_fan_duty_all(FAN_MAX_DUTY, FAN_CPU_ZONES)
            commands.set_fan_duty_all(duty, FAN_NPU_ZONES)
            logger.info("[FanController] fan duty -> %d%%", duty)
            self._applied_duty = duty
        except Exception as e:
            logger.error("[FanController] set fan duty %d%% failed: %s", duty, e)

    def _log_row(self, mode: str, temps: dict[str, float], target: float | None) -> None:
        tag = self._current_tag.tag
        hottest = max(temps.values()) if temps else None
        group = _group_temp(temps)
        append_row(
            self._csv_path,
            _FAN_KEYS,
            {
                "timestamp": now_iso(),
                "stage_type": tag.stage_type,
                "workload_code": tag.workload_code,
                "phase": tag.phase,
                "state": tag.state,
                "iteration_index": tag.iteration_index,
                "mode": mode,
                "max_temp": "" if hottest is None else f"{hottest:.1f}",
                "over_target": "" if hottest is None or target is None else f"{hottest - target:+.1f}",
                "duty": "" if self._applied_duty is None else self._applied_duty,
                "group_temp": "" if group is None else f"{group:.1f}",
                "group_over_target": "" if group is None or target is None else f"{group - target:+.1f}",
            },
        )


def _group_temp(temps: dict[str, float]) -> float | None:
    """Mean Card Temp of the cards without the hottest and coldest one; all cards when two or fewer."""
    valid = sorted(temp for temp in temps.values() if temp > 0)
    if not valid:
        return None
    kept = valid[1:-1] if len(valid) > 2 else valid
    return sum(kept) / len(kept)


def _over_safety_temp(temp: float | None) -> bool:
    return SWEEP_SAFETY_TEMP_C is not None and temp is not None and temp >= SWEEP_SAFETY_TEMP_C

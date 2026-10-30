"""Fan controller — NPU fan zone duty per workload run, switched by main.py:

  MAX     FAN_MAX_DUTY.
  TARGET  the law named in ``start_target`` (target_control/<name>.py) steers a group temp to an
          absolute target; group = mean of the cards without the hottest and coldest one (all
          cards when two or fewer), over Card Temp (CARD) or each card's hottest chiplet HBM temp (HBM).
  FIXED   the given duty.
  GUARD   FAN_MAX_DUTY once any card's Card Temp reaches SAFETY_CARD_TEMP_C or its HBM temp
          reaches SAFETY_HBM_TEMP_C in a TARGET or FIXED run; that run resumes once every card is
          SAFETY_RELEASE_C below both limits (a TARGET law keeps tracking at max fan meanwhile).
HW_CFG and COOL_DOWN run at FAN_MAX_DUTY; CPU zones stay at FAN_MAX_DUTY all session.
Card/HBM temps come from the NPU collectors (no separate poll).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import commands
from common import BaseWorker, CardTempRegistry, CurrentRowTag, append_row, ensure_header, now_iso
from fan_control import target_control
from fan_control.constants import (
    FAN_CPU_ZONES,
    FAN_MAX_DUTY,
    FAN_MIN_DUTY,
    FAN_NPU_ZONES,
    FAN_POLL_INTERVAL_SEC,
)
from test_config import SAFETY_CARD_TEMP_C, SAFETY_HBM_TEMP_C, SAFETY_RELEASE_C

logger = logging.getLogger(__name__)

CARD = "card"
HBM = "hbm"

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
    "target_sensor",
    "target_c",
    "max_card_temp",
    "group_card_temp",
    "max_hbm_temp",
    "group_hbm_temp",
    "max_over_target_c",
    "group_over_target_c",
    "fan_duty_pct",
)
_FAN_KEYS: tuple[str, ...] = (
    "timestamp",
    "stage_type",
    "workload_code",
    "phase",
    "state",
    "iteration_index",
    "mode",
    "sensor",
    "target",
    "max_card",
    "group_card",
    "max_hbm",
    "group_hbm",
    "max_over",
    "group_over",
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
        self._mode = _MAX_MODE
        self._guarded_mode = _MAX_MODE
        self._fixed_duty = FAN_MAX_DUTY
        self._sensor = CARD
        self._target: float | None = None
        self._target_control: target_control.TargetControl | None = None
        self._wanted_duty = FAN_MAX_DUTY
        self._applied_duty: int | None = None
        self._cpu_zones_at_max = False
        ensure_header(self._csv_path, _FAN_HEADER)

    def hold_max(self) -> None:
        with self._lock:
            self._mode = _MAX_MODE

    def hold_duty(self, duty: int) -> None:
        with self._lock:
            self._mode = _FIXED_MODE
            self._fixed_duty = duty

    def start_target(self, control: str, sensor: str, target_c: float) -> None:
        with self._lock:
            logger.info("[FanController] TARGET — %s holds the group %s temp at %.1f°C", control, sensor, target_c)
            self._mode = _TARGET_MODE
            self._sensor = sensor
            self._target = target_c
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
            temps = {CARD: self._card_temps.snapshot(), HBM: self._card_temps.hbm_snapshot()}
            with self._lock:
                mode = self._update_wanted_duty(now, temps)
                sensor, target = (self._sensor, self._target) if mode == _TARGET_MODE else (None, None)
            if self._wanted_duty != self._applied_duty:
                self._apply(self._wanted_duty)
            self._log_row(mode, temps, sensor, target)
            if stop_event.wait(FAN_POLL_INTERVAL_SEC):
                break

    def _update_wanted_duty(self, now: float, temps: dict[str, dict[str, float]]) -> str:
        if self._mode in (_TARGET_MODE, _FIXED_MODE) and (reason := _at_or_over(temps, 0)):
            logger.warning(
                "[FanController] %s — %s run held at %d%% until every card is %g°C below the limits",
                reason,
                self._mode,
                FAN_MAX_DUTY,
                SAFETY_RELEASE_C,
            )
            self._guarded_mode = self._mode
            self._mode = _GUARD_MODE
        elif self._mode == _GUARD_MODE and _released(temps):
            logger.info(
                "[FanController] every card %g°C below the limits — %s run resumes",
                SAFETY_RELEASE_C,
                self._guarded_mode,
            )
            self._mode = self._guarded_mode
        law_mode = self._guarded_mode if self._mode == _GUARD_MODE else self._mode
        if law_mode == _TARGET_MODE:
            self._step_toward_target(now, temps[self._sensor])
        if self._mode != _TARGET_MODE:
            self._wanted_duty = self._fixed_duty if self._mode == _FIXED_MODE else FAN_MAX_DUTY
        return self._mode

    def _step_toward_target(self, now: float, temps: dict[str, float]) -> None:
        group = _group_temp(temps)
        if group is None or self._target is None or self._target_control is None:
            return
        duty = self._target_control.update(now, {_GROUP: group}, {_GROUP: self._target}, self._wanted_duty)
        self._wanted_duty = max(FAN_MIN_DUTY, min(FAN_MAX_DUTY, duty))

    def _apply(self, duty: int) -> None:
        try:
            if not self._cpu_zones_at_max:
                commands.set_fan_duty_all(FAN_MAX_DUTY, FAN_CPU_ZONES)
                self._cpu_zones_at_max = True
            commands.set_fan_duty_all(duty, FAN_NPU_ZONES)
            logger.info("[FanController] fan duty -> %d%%", duty)
            self._applied_duty = duty
        except Exception as e:
            logger.error("[FanController] set fan duty %d%% failed: %s", duty, e)

    def _log_row(
        self, mode: str, temps: dict[str, dict[str, float]], sensor: str | None, target: float | None
    ) -> None:
        tag = self._current_tag.tag
        card, hbm = temps[CARD], temps[HBM]
        controlled = temps[sensor] if sensor else {}
        hottest = max(controlled.values()) if controlled else None
        group = _group_temp(controlled)
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
                "sensor": sensor or "",
                "target": _fmt(target),
                "max_card": _fmt(max(card.values()) if card else None),
                "group_card": _fmt(_group_temp(card)),
                "max_hbm": _fmt(max(hbm.values()) if hbm else None),
                "group_hbm": _fmt(_group_temp(hbm)),
                "max_over": "" if hottest is None or target is None else f"{hottest - target:+.1f}",
                "group_over": "" if group is None or target is None else f"{group - target:+.1f}",
                "duty": "" if self._applied_duty is None else self._applied_duty,
            },
        )


def _group_temp(temps: dict[str, float]) -> float | None:
    """Mean temp of the cards without the hottest and coldest one; all cards when two or fewer."""
    valid = sorted(temp for temp in temps.values() if temp > 0)
    if not valid:
        return None
    kept = valid[1:-1] if len(valid) > 2 else valid
    return sum(kept) / len(kept)


def _fmt(value: float | None) -> str:
    return "" if value is None else f"{value:.1f}"


def _at_or_over(temps: dict[str, dict[str, float]], margin: float) -> str | None:
    """First card at or above ``margin`` below SAFETY_CARD_TEMP_C (Card Temp) or SAFETY_HBM_TEMP_C
    (hottest chiplet HBM), if any."""
    for sensor, limit in ((CARD, SAFETY_CARD_TEMP_C), (HBM, SAFETY_HBM_TEMP_C)):
        if limit is None:
            continue
        for bus, temp in sorted(temps[sensor].items()):
            if temp >= limit - margin:
                return f"{bus} {sensor} {temp:.0f}°C ≥ {limit - margin:g}°C"
    return None


def _released(temps: dict[str, dict[str, float]]) -> bool:
    return bool(temps[CARD] or temps[HBM]) and _at_or_over(temps, SAFETY_RELEASE_C) is None

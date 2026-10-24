"""Fan controller — maps the max HBM temp (CL0..CL3) to a fan duty via DUTY_TRIGGERS.

Runs continuously across the whole session. It reads the latest max HBM temps
published by the NPU collectors (no separate thermal poll), takes the max across
devices, resolves the duty tier, and only re-issues the IPMI fan command when the
tier changes. With no fresh readings (e.g. while hw_cfg resets the devices) it
holds the last duty.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import commands
from common import BaseWorker, HbmTempRegistry, append_row, ensure_header, now_iso
from constants import DUTY_TRIGGERS, FAN_POLL_INTERVAL_SEC, FAN_ZONES

logger = logging.getLogger(__name__)

_FAN_HEADER: tuple[str, ...] = ("Timestamp", "max_hbm_temp", "applied_fan_duty_pct")
_FAN_KEYS: tuple[str, ...] = ("timestamp", "max_temp", "duty")


class FanController(BaseWorker):
    def __init__(
        self, registry: HbmTempRegistry, out_dir: Path, stop_event: threading.Event
    ) -> None:
        super().__init__("fan-controller", stop_event)
        self._registry = registry
        self._csv_path = out_dir / "fan_control.csv"
        self._last_duty: int | None = None
        ensure_header(self._csv_path, _FAN_HEADER)

    def run(self, stop_event: threading.Event) -> None:
        logger.info("[FanController] started — triggers=%s zones=%s", DUTY_TRIGGERS, [hex(z) for z in FAN_ZONES])
        while not stop_event.is_set():
            max_temp = self._registry.max_temp()
            if max_temp is not None:
                self._apply(_duty_for_temp(max_temp), max_temp)
            self._log_row(max_temp)
            if stop_event.wait(FAN_POLL_INTERVAL_SEC):
                break

    def _apply(self, duty: int, max_temp: float) -> None:
        if duty == self._last_duty:
            return
        try:
            commands.set_fan_duty_all(duty, FAN_ZONES)
            logger.info("[FanController] fan duty -> %d%% (max HBM = %.1f°C)", duty, max_temp)
            self._last_duty = duty
        except Exception as e:
            logger.error("[FanController] set duty %d%% failed: %s", duty, e)

    def _log_row(self, max_temp: Any) -> None:
        append_row(
            self._csv_path,
            _FAN_KEYS,
            {
                "timestamp": now_iso(),
                "max_temp": "" if max_temp is None else f"{max_temp:.1f}",
                "duty": "" if self._last_duty is None else self._last_duty,
            },
        )


def _duty_for_temp(temp: float) -> int:
    """Duty of the highest tier whose threshold <= temp; the None-threshold (first)
    entry is the default used below every real threshold. Assumes real thresholds
    are ascending."""
    duty = DUTY_TRIGGERS[0][1]  # default duty (None-threshold entry)
    for threshold, tier_duty in DUTY_TRIGGERS:
        if threshold is not None and temp >= threshold:
            duty = tier_duty
    return duty

"""pi — velocity-form PI on predicted error (Card Temp − target + tau × slope), gain-scheduled."""

from __future__ import annotations

from collections import deque
from itertools import pairwise
from statistics import median

from fan_control.constants import FAN_MAX_DUTY, FAN_MIN_DUTY
from fan_control.target_control import TargetControl


class PiControl(TargetControl):
    """Every INTERVAL_SEC step the duty toward zero predicted error, once the load has started."""

    INTERVAL_SEC = 10
    LOAD_RISE_C = 3.0
    SETTLE_SEC = 8
    SLOPE_WINDOW_SEC = 45
    MIN_SPAN_SEC = 25
    MEDIAN_SAMPLES = 7
    GAIN = 0.5
    HORIZON = 0.5
    MAX_STEP_PCT = 20
    DEADBAND_C = 1.0
    TAU_SEC: tuple[tuple[int, float], ...] = (
        (20, 100.0), (30, 80.0), (40, 44.5), (50, 38.0), (60, 35.0),
        (70, 33.0), (80, 30.0), (90, 28.0), (100, 26.5),
    )
    SENSITIVITY_C_PER_PCT: tuple[tuple[int, float], ...] = (
        (20, 1.13), (35, 1.13), (45, 0.54), (55, 0.31), (65, 0.18),
        (75, 0.12), (85, 0.09), (95, 0.03), (100, 0.03),
    )

    def __init__(self) -> None:
        self._samples: dict[str, deque[tuple[float, float]]] = {}
        self._idle: dict[str, float] = {}
        self._loaded = False
        self._next_control = 0.0
        self._quiet_from = 0.0

    def reset(self, now: float, duty: int) -> None:
        self._samples.clear()
        self._idle.clear()
        self._loaded = False
        self._next_control = now
        self._quiet_from = now

    def update(
        self, now: float, temps: dict[str, float], targets: dict[str, float], duty: int
    ) -> int:
        valid = {bus: temp for bus, temp in temps.items() if bus in targets and temp > 0}
        if not valid:
            return duty
        self._remember(now, valid)
        if not self._loaded:
            if not self._load_started(valid):
                return duty
            self._loaded = True
            self._quiet_from = now
            self._next_control = now
        if now < self._next_control:
            return duty
        errors = self._errors(valid, targets, duty)
        if errors is None:
            return duty
        self._next_control = now + self.INTERVAL_SEC
        error, predicted = errors
        if abs(predicted) <= self.DEADBAND_C:
            return duty
        if abs(error) > self.DEADBAND_C and error * predicted < 0:
            return duty
        new_duty = self._next_duty(duty, predicted)
        if new_duty != duty:
            self._quiet_from = now
        return new_duty

    def _remember(self, now: float, valid: dict[str, float]) -> None:
        for bus, temp in valid.items():
            samples = self._samples.setdefault(bus, deque())
            samples.append((now, temp))
            while now - samples[0][0] > self.SLOPE_WINDOW_SEC:
                samples.popleft()

    def _load_started(self, valid: dict[str, float]) -> bool:
        """True once any card's recent median Card Temp is LOAD_RISE_C above its lowest one."""
        started = False
        for bus in valid:
            recent = median(p[1] for p in list(self._samples[bus])[-self.MEDIAN_SAMPLES :])
            self._idle[bus] = min(self._idle.get(bus, recent), recent)
            started = started or recent >= self._idle[bus] + self.LOAD_RISE_C
        return started

    def _errors(
        self, valid: dict[str, float], targets: dict[str, float], duty: int
    ) -> tuple[float, float] | None:
        """Worst current and worst predicted (current + HORIZON × tau × slope) error over the cards."""
        horizon = self.HORIZON * _interp(self.TAU_SEC, duty)
        current, predicted = [], []
        for bus in valid:
            points = [p for p in self._samples[bus] if p[0] >= self._quiet_from + self.SETTLE_SEC]
            if len(points) < 2 * self.MEDIAN_SAMPLES or points[-1][0] - points[0][0] < self.MIN_SPAN_SEC:
                continue
            head, tail = points[: self.MEDIAN_SAMPLES], points[-self.MEDIAN_SAMPLES :]
            t0, temp0 = median(p[0] for p in head), median(p[1] for p in head)
            t1, temp1 = median(p[0] for p in tail), median(p[1] for p in tail)
            slope = (temp1 - temp0) / (t1 - t0)
            current.append(temp1 - targets[bus])
            predicted.append(temp1 - targets[bus] + horizon * slope)
        return (max(current), max(predicted)) if current else None

    def _next_duty(self, duty: int, error_c: float) -> int:
        """Newton-like duty step toward zero predicted error, scaled by the duty's sensitivity."""
        step = self.GAIN * error_c / _interp(self.SENSITIVITY_C_PER_PCT, duty)
        step = max(-self.MAX_STEP_PCT, min(self.MAX_STEP_PCT, step))
        step_pct = round(step) or (1 if error_c > 0 else -1)
        return max(FAN_MIN_DUTY, min(FAN_MAX_DUTY, duty + step_pct))


def _interp(table: tuple[tuple[int, float], ...], duty: float) -> float:
    if duty <= table[0][0]:
        return table[0][1]
    for (d0, v0), (d1, v1) in pairwise(table):
        if duty <= d1:
            return v0 + (v1 - v0) * (duty - d0) / (d1 - d0)
    return table[-1][1]

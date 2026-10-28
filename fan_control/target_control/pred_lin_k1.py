"""pred_lin_k1 — Card Temp HORIZON_SEC ahead on its least-squares trend; duty steps GAIN_PCT_PER_C × ΔT."""

from __future__ import annotations

from collections import deque
from statistics import median

from fan_control.constants import FAN_MAX_DUTY, FAN_MIN_DUTY
from fan_control.target_control import TargetControl


class PredLinControl(TargetControl):
    """Every INTERVAL_SEC step the duty by the worst card's predicted error, once the load has started."""

    INTERVAL_SEC = 10
    LOAD_RISE_C = 3.0
    MEDIAN_SAMPLES = 7
    TREND_WINDOW_SEC = 30
    MIN_SAMPLES = 10
    HORIZON_SEC = 30.0
    GAIN_PCT_PER_C = 1.0
    MAX_STEP_PCT = 20
    DEADBAND_C = 0.5

    def __init__(self) -> None:
        self._samples: dict[str, deque[tuple[float, float]]] = {}
        self._idle: dict[str, float] = {}
        self._loaded = False
        self._next_control = 0.0

    def reset(self, now: float, duty: int) -> None:
        self._samples.clear()
        self._idle.clear()
        self._loaded = False
        self._next_control = now

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
            self._next_control = now
        if now < self._next_control:
            return duty
        predicted = {bus: self._predict(self._samples[bus]) for bus in valid}
        errors = [pred - targets[bus] for bus, pred in predicted.items() if pred is not None]
        if not errors:
            return duty
        self._next_control = now + self.INTERVAL_SEC
        return step_duty(duty, max(errors), self.GAIN_PCT_PER_C, self.MAX_STEP_PCT, self.DEADBAND_C)

    def _remember(self, now: float, valid: dict[str, float]) -> None:
        for bus, temp in valid.items():
            samples = self._samples.setdefault(bus, deque())
            samples.append((now, temp))
            while now - samples[0][0] > self.TREND_WINDOW_SEC:
                samples.popleft()

    def _load_started(self, valid: dict[str, float]) -> bool:
        """True once any card's recent median Card Temp is LOAD_RISE_C above its lowest one."""
        started = False
        for bus in valid:
            recent = median(p[1] for p in list(self._samples[bus])[-self.MEDIAN_SAMPLES :])
            self._idle[bus] = min(self._idle.get(bus, recent), recent)
            started = started or recent >= self._idle[bus] + self.LOAD_RISE_C
        return started

    def _predict(self, samples: deque[tuple[float, float]]) -> float | None:
        """T0 + slope × HORIZON_SEC, with T0 and slope from a least-squares line over TREND_WINDOW_SEC."""
        if len(samples) < self.MIN_SAMPLES:
            return None
        t_mean = sum(p[0] for p in samples) / len(samples)
        y_mean = sum(p[1] for p in samples) / len(samples)
        sxx = sum((p[0] - t_mean) ** 2 for p in samples)
        if sxx == 0:
            return None
        slope = sum((p[0] - t_mean) * (p[1] - y_mean) for p in samples) / sxx
        t0 = y_mean + slope * (samples[-1][0] - t_mean)
        return t0 + slope * self.HORIZON_SEC


def step_duty(duty: int, error_c: float, gain: float, max_step: int, deadband_c: float) -> int:
    """Duty moved by gain × error, none within ``deadband_c`` and at most ``max_step`` either way."""
    if abs(error_c) <= deadband_c:
        return duty
    step = max(-max_step, min(max_step, round(gain * error_c)))
    return max(FAN_MIN_DUTY, min(FAN_MAX_DUTY, duty + step))

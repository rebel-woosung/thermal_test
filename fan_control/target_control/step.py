"""step — velocity-form I control: every INTERVAL_SEC the duty moves by the worst card's error."""

from __future__ import annotations

from fan_control.constants import FAN_MAX_DUTY, FAN_MIN_DUTY
from fan_control.target_control import TargetControl


class StepControl(TargetControl):
    """Every INTERVAL_SEC move the duty by the worst card's error, capped and deadbanded."""

    INTERVAL_SEC = 10
    GAIN_PCT_PER_C = 1.0
    MAX_STEP_PCT = 5
    DEADBAND_C = 1.0

    def __init__(self) -> None:
        self._next_control = 0.0

    def reset(self, now: float, duty: int) -> None:
        self._next_control = now

    def update(
        self, now: float, temps: dict[str, float], targets: dict[str, float], duty: int
    ) -> int:
        if now < self._next_control:
            return duty
        self._next_control = now + self.INTERVAL_SEC
        worst = max(temps[bus] - target for bus, target in targets.items() if bus in temps)
        return self._step_duty(duty, worst)

    def _step_duty(self, duty: int, over_target_c: float) -> int:
        """Step the duty up while the hottest-rising card is above its target, down while below."""
        if abs(over_target_c) <= self.DEADBAND_C:
            return duty
        step = round(over_target_c * self.GAIN_PCT_PER_C)
        step = max(-self.MAX_STEP_PCT, min(self.MAX_STEP_PCT, step))
        return max(FAN_MIN_DUTY, min(FAN_MAX_DUTY, duty + step))

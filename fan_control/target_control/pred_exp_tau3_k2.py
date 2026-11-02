"""pred_exp_tau3_k2 — pred_exp_k2 on TAU_SEC at 10 / 20 % re-fit from the CR13 fan-target run, 1 °C deadband,
not below WARM_UP_MIN_DUTY for the first WARM_UP_SEC of load, then lowered at most RELEASE_MAX_DROP_PCT a step
until the group temp first reaches its target, after which the Kalman filter assumes SETTLED_SENSOR_VAR_C2
(same as the engine's _PredExpK2)."""

from __future__ import annotations

from fan_control.target_control.exp import _Card
from fan_control.target_control.pred_exp_k2 import PredExpK2Control


class PredExpTau3K2Control(PredExpK2Control):
    """pred_exp_k2 with TAU_SEC 60 s at 10 % and 40 s at 20 % (30 % and up unchanged), DEADBAND_C 1.0,
    a WARM_UP_MIN_DUTY floor for the first WARM_UP_SEC after the load starts and RELEASE_MAX_DROP_PCT
    steps down after it until the target is first reached, then SETTLED_SENSOR_VAR_C2."""

    DEADBAND_C = 1.0
    WARM_UP_SEC = 120
    WARM_UP_MIN_DUTY = 30
    RELEASE_MAX_DROP_PCT = 5
    SETTLED_SENSOR_VAR_C2 = 1.0
    TAU_SEC: tuple[tuple[int, float], ...] = (
        (10, 60.0), (20, 40.0), (30, 73.0), (40, 49.0), (50, 38.0), (60, 29.0),
        (70, 25.0), (80, 21.0), (90, 20.0), (100, 20.0),
    )

    def reset(self, now: float, duty: int) -> None:
        super().reset(now, duty)
        self._now = now
        self._load_started_at: float | None = None
        self._target_reached = False
        self.SENSOR_VAR_C2 = type(self).SENSOR_VAR_C2

    def update(self, now: float, temps: dict[str, float], targets: dict[str, float], duty: int) -> int:
        self._now = now
        if not self._target_reached and any(temps[bus] >= target for bus, target in targets.items() if bus in temps):
            self._target_reached = True
            self.SENSOR_VAR_C2 = self.SETTLED_SENSOR_VAR_C2
        return super().update(now, temps, targets, duty)

    def _choose(self, cards: list[tuple[_Card, float]], duty: int) -> int:
        if self._load_started_at is None:
            self._load_started_at = self._now
        chosen = super()._choose(cards, duty)
        if self._now - self._load_started_at < self.WARM_UP_SEC:
            return max(self.WARM_UP_MIN_DUTY, chosen)
        if not self._target_reached:
            return max(chosen, duty - self.RELEASE_MAX_DROP_PCT)
        return chosen

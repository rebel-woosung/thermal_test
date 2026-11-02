"""pred_exp_tau3_k2 — pred_exp_k2 on TAU_SEC at 10 / 20 % re-fit from the CR13 fan-target run, 1 °C deadband,
not below WARM_UP_MIN_DUTY for the first WARM_UP_SEC of load (same as the engine's _PredExpK2)."""

from __future__ import annotations

from fan_control.target_control.exp import _Card
from fan_control.target_control.pred_exp_k2 import PredExpK2Control


class PredExpTau3K2Control(PredExpK2Control):
    """pred_exp_k2 with TAU_SEC 60 s at 10 % and 40 s at 20 % (30 % and up unchanged), DEADBAND_C 1.0
    and a WARM_UP_MIN_DUTY floor for the first WARM_UP_SEC after the load starts."""

    DEADBAND_C = 1.0
    WARM_UP_SEC = 180
    WARM_UP_MIN_DUTY = 30
    TAU_SEC: tuple[tuple[int, float], ...] = (
        (10, 60.0), (20, 40.0), (30, 73.0), (40, 49.0), (50, 38.0), (60, 29.0),
        (70, 25.0), (80, 21.0), (90, 20.0), (100, 20.0),
    )

    def reset(self, now: float, duty: int) -> None:
        super().reset(now, duty)
        self._now = now
        self._load_started_at: float | None = None

    def update(self, now: float, temps: dict[str, float], targets: dict[str, float], duty: int) -> int:
        self._now = now
        return super().update(now, temps, targets, duty)

    def _choose(self, cards: list[tuple[_Card, float]], duty: int) -> int:
        if self._load_started_at is None:
            self._load_started_at = self._now
        chosen = super()._choose(cards, duty)
        warming_up = self._now - self._load_started_at < self.WARM_UP_SEC
        return max(self.WARM_UP_MIN_DUTY, chosen) if warming_up else chosen

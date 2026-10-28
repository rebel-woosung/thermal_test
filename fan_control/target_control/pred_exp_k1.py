"""pred_exp_k1 — Card Temp HORIZON_SEC ahead as T∞ + (T0 − T∞)·e^(−t/τ); duty steps as in pred_lin_k1."""

from __future__ import annotations

from fan_control.target_control.exp import ExpControl, _Card
from fan_control.target_control.pred_lin_k1 import step_duty


class PredExpControl(ExpControl):
    """Every INTERVAL_SEC step the duty by the worst card's predicted error, once the load has started."""

    HORIZON_SEC = 30.0
    GAIN_PCT_PER_C = 1.0
    MAX_STEP_PCT = 20
    DEADBAND_C = 0.5

    def _choose(self, cards: list[tuple[_Card, float]], duty: int) -> int:
        worst = max(
            self._ahead(card.temp, card.asym, duty, self.HORIZON_SEC) - target for card, target in cards
        )
        return step_duty(duty, worst, self.GAIN_PCT_PER_C, self.MAX_STEP_PCT, self.DEADBAND_C)

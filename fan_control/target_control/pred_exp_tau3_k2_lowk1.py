"""pred_exp_tau3_k2_lowk1 — pred_exp_tau3_k2 stepping 1 % per °C at or below LOW_DUTY_PCT."""

from __future__ import annotations

from fan_control.target_control.exp import _Card
from fan_control.target_control.pred_exp_tau3_k2 import PredExpTau3K2Control
from fan_control.target_control.pred_lin_k1 import step_duty


class PredExpTau3K2LowK1Control(PredExpTau3K2Control):
    """pred_exp_tau3_k2 with LOW_DUTY_GAIN_PCT_PER_C while the duty is at or below LOW_DUTY_PCT."""

    LOW_DUTY_PCT = 30
    LOW_DUTY_GAIN_PCT_PER_C = 1.0

    def _choose(self, cards: list[tuple[_Card, float]], duty: int) -> int:
        worst = max(
            self._ahead(card.temp, card.asym, duty, self.HORIZON_SEC) - target for card, target in cards
        )
        gain = self.LOW_DUTY_GAIN_PCT_PER_C if duty <= self.LOW_DUTY_PCT else self.GAIN_PCT_PER_C
        return step_duty(duty, worst, gain, self.MAX_STEP_PCT, self.DEADBAND_C)

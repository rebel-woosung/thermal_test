"""pred_lin_to_exp — pred_lin_k1 until the Card Temp first reaches target − REACH_C, pred_exp_k1_5 after."""

from __future__ import annotations

import logging

from fan_control.target_control import TargetControl
from fan_control.target_control.pred_exp_k1_5 import PredExpK15Control
from fan_control.target_control.pred_lin_k1 import PredLinControl

logger = logging.getLogger(__name__)


class PredLinToExpControl(TargetControl):
    """Both laws track every poll; the duty comes from pred_lin_k1, then from pred_exp_k1_5 once reached."""

    REACH_C = 0.5

    def __init__(self) -> None:
        self._approach = PredLinControl()
        self._hold = PredExpK15Control()
        self._reached = False

    def reset(self, now: float, duty: int) -> None:
        self._approach.reset(now, duty)
        self._hold.reset(now, duty)
        self._reached = False

    def update(
        self, now: float, temps: dict[str, float], targets: dict[str, float], duty: int
    ) -> int:
        approach = self._approach.update(now, temps, targets, duty)
        hold = self._hold.update(now, temps, targets, duty)
        if not self._reached and any(
            temps.get(bus, 0) >= target - self.REACH_C for bus, target in targets.items()
        ):
            self._reached = True
            logger.info("[pred_lin_to_exp] target reached at %d%% — switching to pred_exp_k1_5", duty)
        return hold if self._reached else approach

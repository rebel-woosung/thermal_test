"""mpc — receding-horizon search on exp's model: a duty now, then the settling duty, over HORIZON_SEC."""

from __future__ import annotations

import math

from fan_control.constants import FAN_MAX_DUTY, FAN_MIN_DUTY
from fan_control.target_control.exp import ExpControl, _Card


class MpcControl(ExpControl):
    """Every INTERVAL_SEC pick the duty that, held until the next step takes effect and then followed by the
    settling duty, minimises the worst card's squared error over HORIZON_SEC plus CHANGE_COST_C2 per change."""

    HORIZON_SEC = 120
    ACTUATION_LAG_SEC = 5.0
    OVERSHOOT_WEIGHT = 30.0
    CHANGE_COST_C2 = 0.3

    def _choose(self, cards: list[tuple[_Card, float]], duty: int) -> int:
        settle = max(self._settle_duty(card, target, duty) for card, target in cards)
        best, best_cost = duty, math.inf
        for candidate in range(FAN_MIN_DUTY, FAN_MAX_DUTY + 1):
            if any(self._asym_at(card, candidate, duty) > t + self.BOOST_MAX_C for card, t in cards):
                continue
            cost = self._cost(cards, duty, candidate, settle)
            if candidate != duty:
                cost += self.CHANGE_COST_C2
            if cost < best_cost:
                best, best_cost = candidate, cost
        return best

    def _settle_duty(self, card: _Card, target: float, duty: int) -> int:
        """Lowest duty whose asymptote is at or below ``target``."""
        for candidate in range(FAN_MIN_DUTY, FAN_MAX_DUTY + 1):
            if self._asym_at(card, candidate, duty) <= target:
                return candidate
        return FAN_MAX_DUTY

    def _cost(self, cards: list[tuple[_Card, float]], duty: int, first: int, settle: int) -> float:
        """Mean squared worst-card error (overshoot × OVERSHOOT_WEIGHT) every INTERVAL_SEC to HORIZON_SEC."""
        hold = self.INTERVAL_SEC + self.ACTUATION_LAG_SEC
        worst = [-math.inf] * (self.HORIZON_SEC // self.INTERVAL_SEC)
        for card, target in cards:
            temp = self._ahead(card.temp, self._asym_at(card, first, duty), first, hold)
            asym = self._asym_at(card, settle, duty)
            for k in range(len(worst)):
                worst[k] = max(worst[k], self._ahead(temp, asym, settle, k * self.INTERVAL_SEC) - target)
        return sum(e * e * (self.OVERSHOOT_WEIGHT if e > 0 else 1.0) for e in worst) / len(worst)

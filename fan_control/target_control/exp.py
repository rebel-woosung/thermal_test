"""exp — Card Temp follows target − (target − T)·e^(−t / TAU_CL_SEC) on a Kalman-tracked first-order model."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from statistics import median

from fan_control.constants import FAN_MAX_DUTY, FAN_MIN_DUTY
from fan_control.target_control import TargetControl
from fan_control.target_control.pi import _interp


@dataclass
class _Card:
    """Kalman estimate of one card's Card Temp and the temp it heads to at the current duty."""

    time: float
    temp: float
    asym: float
    p00: float
    p01: float
    p11: float


class ExpControl(TargetControl):
    """Every INTERVAL_SEC pick the lowest duty whose predicted Card Temp stays on the path to target."""

    INTERVAL_SEC = 10
    LOAD_RISE_C = 3.0
    MEDIAN_SAMPLES = 7
    TAU_CL_SEC = 30.0
    DEADBAND_C = 0.5
    BOOST_MAX_C = 15.0
    AMBIENT_C = 25.0
    SENSOR_VAR_C2 = 0.45
    DRIFT_VAR_C2_PER_SEC = 0.005
    MODEL_ERROR = 0.1
    INITIAL_VAR_C2 = 100.0
    TAU_SEC: tuple[tuple[int, float], ...] = (
        (20, 100.0), (30, 73.0), (40, 49.0), (50, 38.0), (60, 29.0),
        (70, 25.0), (80, 21.0), (90, 20.0), (100, 20.0),
    )
    RESISTANCE: tuple[tuple[int, float], ...] = (
        (20, 2.38), (30, 1.93), (40, 1.48), (50, 1.28), (60, 1.16),
        (70, 1.09), (80, 1.04), (90, 1.01), (100, 1.0),
    )

    def __init__(self) -> None:
        self._recent: dict[str, deque[float]] = {}
        self._idle: dict[str, float] = {}
        self._cards: dict[str, _Card] = {}
        self._loaded = False
        self._duty = FAN_MAX_DUTY
        self._next_control = 0.0

    def reset(self, now: float, duty: int) -> None:
        self._recent.clear()
        self._idle.clear()
        self._cards.clear()
        self._loaded = False
        self._duty = duty
        self._next_control = now

    def update(
        self, now: float, temps: dict[str, float], targets: dict[str, float], duty: int
    ) -> int:
        valid = {bus: temp for bus, temp in temps.items() if bus in targets and temp > 0}
        if not valid:
            return duty
        if not self._loaded:
            if not self._load_started(valid):
                return duty
            self._loaded = True
            self._next_control = now
        if duty != self._duty:
            for card in self._cards.values():
                self._remap(card, self._duty, duty)
            self._duty = duty
        for bus, temp in valid.items():
            if bus in self._cards:
                self._track(self._cards[bus], now, temp, duty)
            else:
                self._cards[bus] = _Card(now, temp, temp, self.SENSOR_VAR_C2, 0.0, self.INITIAL_VAR_C2)
        if now < self._next_control:
            return duty
        self._next_control = now + self.INTERVAL_SEC
        return self._choose([(self._cards[bus], targets[bus]) for bus in valid], duty)

    def _choose(self, cards: list[tuple[_Card, float]], duty: int) -> int:
        """The duty the card asking for the most cooling needs."""
        return max(self._card_duty(card, target, duty) for card, target in cards)

    def _load_started(self, valid: dict[str, float]) -> bool:
        """True once any card's recent median Card Temp is LOAD_RISE_C above its lowest one."""
        started = False
        for bus, temp in valid.items():
            recent = self._recent.setdefault(bus, deque(maxlen=self.MEDIAN_SAMPLES))
            recent.append(temp)
            level = median(recent)
            self._idle[bus] = min(self._idle.get(bus, level), level)
            started = started or level >= self._idle[bus] + self.LOAD_RISE_C
        return started

    def _track(self, card: _Card, now: float, temp: float, duty: int) -> None:
        """Kalman predict over the elapsed time at ``duty``, then correct with the measured Card Temp."""
        dt = max(now - card.time, 0.0)
        card.time = now
        lam = math.exp(-dt / _interp(self.TAU_SEC, duty))
        card.temp = card.asym + (card.temp - card.asym) * lam
        card.p00 = lam * lam * card.p00 + 2 * lam * (1 - lam) * card.p01 + (1 - lam) ** 2 * card.p11
        card.p01 = lam * card.p01 + (1 - lam) * card.p11
        card.p11 += self.DRIFT_VAR_C2_PER_SEC * dt
        k0 = card.p00 / (card.p00 + self.SENSOR_VAR_C2)
        k1 = card.p01 / (card.p00 + self.SENSOR_VAR_C2)
        innovation = temp - card.temp
        card.temp += k0 * innovation
        card.asym += k1 * innovation
        card.p11 -= k1 * card.p01
        card.p01 *= 1 - k0
        card.p00 *= 1 - k0

    def _remap(self, card: _Card, old: int, new: int) -> None:
        """Move the asymptote to the new duty along RESISTANCE; its variance grows by MODEL_ERROR of the move."""
        ratio = _interp(self.RESISTANCE, new) / _interp(self.RESISTANCE, old)
        moved = (card.asym - self.AMBIENT_C) * (ratio - 1)
        card.asym += moved
        card.p01 *= ratio
        card.p11 = card.p11 * ratio * ratio + (self.MODEL_ERROR * moved) ** 2

    def _card_duty(self, card: _Card, target: float, duty: int) -> int:
        """Lowest duty whose Card Temp INTERVAL_SEC ahead is at or below the exponential path to ``target``."""
        if abs(card.asym - target) <= self.DEADBAND_C:
            return duty
        path = target - (target - card.temp) * math.exp(-self.INTERVAL_SEC / self.TAU_CL_SEC)
        for candidate in range(FAN_MIN_DUTY, FAN_MAX_DUTY + 1):
            asym = self._asym_at(card, candidate, duty)
            if asym > target + self.BOOST_MAX_C:
                continue
            if self._ahead(card.temp, asym, candidate, self.INTERVAL_SEC) <= path:
                return candidate
        return FAN_MAX_DUTY

    def _asym_at(self, card: _Card, candidate: int, duty: int) -> float:
        """The card's asymptote at ``candidate``, scaled from the one tracked at ``duty`` along RESISTANCE."""
        ratio = _interp(self.RESISTANCE, candidate) / _interp(self.RESISTANCE, duty)
        return self.AMBIENT_C + (card.asym - self.AMBIENT_C) * ratio

    def _ahead(self, temp: float, asym: float, candidate: int, sec: float) -> float:
        """Card Temp ``sec`` from now at ``candidate``, starting at ``temp`` and heading to ``asym``."""
        return asym + (temp - asym) * math.exp(-sec / _interp(self.TAU_SEC, candidate))

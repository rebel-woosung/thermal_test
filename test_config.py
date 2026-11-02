"""Test conditions varied between thermal-test runs."""

from __future__ import annotations

WORKLOAD_CODES: list[str] = []
TEST_UNIT_IDS: list[int] = [11, 21, 22, 23, 24, 26, 41, 42, 61, 62]
TEST_TIME_SEC: int | None = 1200

FAN_TARGET_CONTROLS: list[str] = ["pred_exp_tau3_k2"]
COOL_DOWN_SEC = 60
SAFETY_CARD_TEMP_C: float | None = 110
SAFETY_HBM_TEMP_C: float | None = 95
SAFETY_RELEASE_C = 5

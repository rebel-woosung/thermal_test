"""Test conditions varied between thermal-test runs."""

from __future__ import annotations

WORKLOAD_CODES: list[str] = []

FAN_TARGET_CONTROL = "pred_exp_tau2_k2_h45"
COOL_DOWN_SEC = 60
SAFETY_CARD_TEMP_C: float | None = 110
SAFETY_HBM_TEMP_C: float | None = 95
SAFETY_RELEASE_C = 5

"""Test conditions varied between thermal-test runs."""

from __future__ import annotations

WORKLOAD_CODES: list[str] = []

MAX_PHASE_SEC = 180
TARGET_PHASE_SEC = 480
SWEEP_STEP_SEC = 300
SWEEP_DUTIES: list[int] = []
SWEEP_UP = False

FAN_TARGET_RISE_C = 10
FAN_TARGET_CONTROLS: list[str] = ["pred_lin_to_exp"]
SWEEP_SAFETY_TEMP_C: float | None = 110

# Rest before each phase, including between workloads.
REST_SEC = 3 * 60

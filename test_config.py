"""Test conditions varied between thermal-test runs."""

from __future__ import annotations

MAX_PHASE_SEC = 300
TARGET_PHASE_SEC = 900
SWEEP_STEP_SEC = 300
SWEEP_DUTIES: list[int] = []

FAN_TARGET_RISE_C = 10
FAN_TARGET_CONTROLS: list[str] = ["pi", "pi_fast"]
SWEEP_SAFETY_TEMP_C: float | None = 110

# Rest before each phase, including between workloads.
REST_SEC = 3 * 60

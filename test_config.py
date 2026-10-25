"""Test conditions varied between thermal-test runs."""

from __future__ import annotations

MAX_PHASE_SEC = 600
TARGET_PHASE_SEC = 600
SWEEP_STEP_SEC = 300
SWEEP_DUTIES: list[int] = [100, 90, 80, 70, 60, 50, 40, 30]

FAN_TARGET_RISE_C = 10
SWEEP_SAFETY_TEMP_C: float | None = 110

# Rest before each phase, including between workloads.
REST_SEC = 3 * 60

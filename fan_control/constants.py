"""Fan constants shared by the controller and every TARGET law (each law keeps its own tunables)."""

from __future__ import annotations

# BMC fan zones. 0x00 NPU1-4, 0x01 NPU5-8, 0x02 CPU1, 0x03 CPU2.
FAN_NPU_ZONES: list[int] = [0x00, 0x01]
FAN_CPU_ZONES: list[int] = [0x02, 0x03]
FAN_ZONES: list[int] = FAN_NPU_ZONES + FAN_CPU_ZONES

FAN_MAX_DUTY = 100
FAN_MIN_DUTY = 20
FAN_BASELINE_WINDOW_SEC = 30

# On exit, drop all fans to this duty as a safe fallback. Set None to leave as-is.
RESTORE_FAN_DUTY_ON_EXIT: int | None = 20

# How often the fan controller samples Card Temp and logs fan_control.csv.
FAN_POLL_INTERVAL_SEC = 1

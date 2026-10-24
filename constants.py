"""Thermal-test tunables and constants. Workloads live in workloads/CR03.json."""

from __future__ import annotations

from pathlib import Path

# --------------------------------------------------------------------------
# Fan control — max fan, then hold Card Temp at baseline + rise (see fan_controller.py)
# --------------------------------------------------------------------------
# BMC fan zones. 0x00 NPU1-4, 0x01 NPU5-8, 0x02 CPU1, 0x03 CPU2.
FAN_NPU_ZONES: list[int] = [0x00, 0x01]
FAN_CPU_ZONES: list[int] = [0x02, 0x03]
FAN_ZONES: list[int] = FAN_NPU_ZONES + FAN_CPU_ZONES

FAN_MAX_DUTY = 100
FAN_MIN_DUTY = 20
FAN_MAX_PHASE_RATIO = 0.5
FAN_BASELINE_WINDOW_SEC = 30
FAN_TARGET_RISE_C = 10
FAN_CONTROL_INTERVAL_SEC = 10
FAN_GAIN_PCT_PER_C = 1.0
FAN_MAX_STEP_PCT = 5
FAN_DEADBAND_C = 1.0

# On exit, drop all fans to this duty as a safe fallback. Set None to leave as-is.
RESTORE_FAN_DUTY_ON_EXIT: int | None = 20

# How often the fan controller samples Card Temp and logs fan_control.csv.
FAN_POLL_INTERVAL_SEC = 1

# --------------------------------------------------------------------------
# Workloads
# --------------------------------------------------------------------------
# Directory holding the (decoded, plain-named) .bin workload files.
# Same path the main engine uses (test_config.json ``workload_dir``).
WORKLOAD_DIR = Path("/data/rbcn/rb-srt-main-engine/workload")

# Main-engine workload config: stages run in engine order, iteration × repeat_count.
WORKLOADS_JSON = Path(__file__).resolve().parent / "workloads" / "CR03.json"

# Rest between consecutive workloads.
REST_SEC = 10 * 60

# --------------------------------------------------------------------------
# Monitoring
# --------------------------------------------------------------------------
NPU_INTERVAL_SEC = 1
SERVER_BMC_INTERVAL_SEC = 5
SMC_SWEEP_PAUSE_SEC = 1.0

# Logs + CSV output — each run writes to a new log/<timestamp>/ folder.
LOG_DIR = Path(__file__).resolve().parent / "log"

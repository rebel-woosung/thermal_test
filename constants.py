"""Thermal-test tunables and constants. Workloads live in workloads/CR03.json."""

from __future__ import annotations

from pathlib import Path

# --------------------------------------------------------------------------
# Fan control — HBM CL0 temp (°C) → fan duty (%)
# --------------------------------------------------------------------------
# (hbm_cl0_temp_threshold_C, fan_duty_percent). The None threshold is the default
# duty applied below the first real threshold. Real thresholds kept ascending.
DUTY_TRIGGERS: list[tuple[int | None, int]] = [
    (None, 20),
    (40, 40),
    (50, 50),
    (60, 60),
    (70, 70),
    (80, 80),
    (90, 90),
    (92, 100),
]

# BMC fan zones set together. 0x00 NPU1-4, 0x01 NPU5-8, 0x02 CPU1, 0x03 CPU2.
FAN_ZONES: list[int] = [0x00, 0x01, 0x02, 0x03]

# On exit, drop fans to the DUTY_TRIGGERS default duty as a safe fallback. Set None to leave as-is.
RESTORE_FAN_DUTY_ON_EXIT: int | None = DUTY_TRIGGERS[0][1]

# How often the fan controller samples HBM CL0 and re-evaluates the duty tier.
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

"""Thermal-test tunables and constants. Workloads live in workloads/CR03.json, fan control in fan_control/."""

from __future__ import annotations

from pathlib import Path

# --------------------------------------------------------------------------
# Workloads
# --------------------------------------------------------------------------
# Directory holding the (decoded, plain-named) .bin workload files.
# Same path the main engine uses (test_config.json ``workload_dir``).
WORKLOAD_DIR = Path("/data/rbcn/rb-srt-main-engine/workload")

# Main-engine workload config: stages run in engine order, iteration × repeat_count.
WORKLOADS_JSON = Path(__file__).resolve().parent / "workloads" / "CR03.json"

# --------------------------------------------------------------------------
# Monitoring
# --------------------------------------------------------------------------
NPU_INTERVAL_SEC = 1
SERVER_BMC_INTERVAL_SEC = 5

# Logs + CSV output — each run writes to a new log/<timestamp>/ folder.
LOG_DIR = Path(__file__).resolve().parent / "log"

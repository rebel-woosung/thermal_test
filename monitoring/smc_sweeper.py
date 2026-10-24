"""OOB SMC PMIC temperature (+ eFuse power) sweeper.

A single long-lived thread serially sweeps every device via rbln-smcif
power_mon/sensor_mon and caches per-device (keep-last-good); the NPU collector
reads the cache instead of doing the slow OOB call inline.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

import commands
from commands import quiet_shell
from common import BaseWorker, Device
from constants import SMC_SWEEP_PAUSE_SEC

logger = logging.getLogger(__name__)


class SmcTempSweeper(BaseWorker):
    def __init__(self, devices: list[Device], stop_event: threading.Event) -> None:
        super().__init__("smc-temp-sweeper", stop_event)
        self._devices = devices
        self._cache: dict[int, dict[str, Any]] = {}

    def run(self, stop_event: threading.Event) -> None:
        logger.info("[SmcTempSweeper] started — %d device(s)", len(self._devices))
        errored_slots: set[int] = set()
        while not stop_event.is_set():
            with quiet_shell():
                for dev in self._devices:
                    if stop_event.is_set():
                        break
                    try:
                        result = self._read_device_snapshot(dev.slot_id)
                        if any(v is not None for v in result.values()):
                            self._cache[dev.device_id] = result
                        errored_slots.discard(dev.slot_id)
                    except Exception as e:
                        self._cache.pop(dev.device_id, None)
                        if dev.slot_id not in errored_slots:
                            logger.error("[SmcTempSweeper] slot %s failed: %s", dev.slot_id, e)
                            errored_slots.add(dev.slot_id)
            stop_event.wait(SMC_SWEEP_PAUSE_SEC)

    def snapshot_for(self, device_id: int) -> dict[str, Any]:
        cached = self._cache.get(device_id)
        return dict(cached) if cached else _empty_snapshot()

    def _read_device_snapshot(self, slot_id: int) -> dict[str, Any]:
        snap = _empty_snapshot()

        power_out = commands.read_smc_power_mon(slot_id)
        for item in json.loads(power_out) if power_out.strip() else []:
            key = f"smc_temp_{item.get('device')}"
            if key in snap:
                snap[key] = item.get("tdie_c")
            if item.get("device") == "eFUSE":
                snap["smc_pwr_eFUSE_volt"] = item.get("volt_mv")
                snap["smc_pwr_eFUSE_curr"] = item.get("curr_ma")

        sensor_out = commands.read_smc_sensor_mon(slot_id)
        data = json.loads(sensor_out) if sensor_out.strip() else {}
        for entry in data.get("temp", []):
            key = f"smc_temp_{entry.get('sensor')}"
            if key in snap:
                snap[key] = entry.get("temp_c")

        return snap


# (internal_key, csv_header); internal_key = "smc_temp_" + JSON device/sensor value.
SMC_TEMP_COLUMNS: tuple[tuple[str, str], ...] = (
    ("smc_temp_TMP0", "Board Temp1"),
    ("smc_temp_TMP1", "Board Temp2"),
    ("smc_temp_eFUSE", "Efuse Temp"),
    ("smc_temp_CORE0_0P75V", "Core0 Temp"),
    ("smc_temp_CORE1_0P75V", "Core1 Temp"),
    ("smc_temp_HBM_0P85V", "HBM Temp"),
    ("smc_temp_SYS_1P8V", "SYS 1.8V Temp"),
    ("smc_temp_UCIE_VDDQ_0P75V", "UCIe Vddq Temp"),
    ("smc_temp_HBM_VDDQ_1P1V", "HBM Vddq Temp"),
    ("smc_temp_HBM_VDDQL_0P4V(T)", "HBM Vddq-top Temp"),
    ("smc_temp_HBM_VDDQL_0P4V(B)", "HBM Vddq-bottom Temp"),
)

OOB_PWR_COLUMNS: tuple[tuple[str, str], ...] = (
    ("smc_pwr_eFUSE_volt", "eFuse(OOB) voltage"),
    ("smc_pwr_eFUSE_curr", "eFuse(OOB) current"),
)


def _empty_snapshot() -> dict[str, Any]:
    return {key: None for key, _ in SMC_TEMP_COLUMNS + OOB_PWR_COLUMNS}

"""OOB SMC temperature sweeper — board TMP0/TMP1 + PMIC rail Tdie via rbln-smcif.

A serial sweep of all cards is slow (~10-40 s / 8 cards), so one thread sweeps and caches
per bus address (keep-last-good); the NPU collectors read the cache instead of calling OOB inline.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

import commands
from commands import quiet_shell
from common import Device

logger = logging.getLogger(__name__)

_SWEEP_PAUSE_SEC = 1.0


class SmcTempSweeper(threading.Thread):
    def __init__(self, devices: list[Device], stop_event: threading.Event) -> None:
        super().__init__(name="smc-temp-sweeper", daemon=False)
        self._devices = [d for d in devices if d.slot_id is not None]
        self._stop_event = stop_event
        self._cache: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self.join(timeout=timeout)

    def snapshot_for(self, bus_address: str) -> dict[str, Any]:
        with self._lock:
            cached = self._cache.get(bus_address)
        return dict(cached) if cached else empty_smc_temp()

    def run(self) -> None:
        logger.info("[SmcTempSweeper] started — %d device(s)", len(self._devices))
        errored: set[str] = set()
        while not self._stop_event.is_set():
            with quiet_shell():
                for dev in self._devices:
                    if self._stop_event.is_set():
                        return
                    try:
                        snap = _read_snapshot(dev.slot_id)
                        if any(v is not None for v in snap.values()):
                            with self._lock:
                                self._cache[dev.bus_address] = snap
                        errored.discard(dev.bus_address)
                    except Exception as e:
                        with self._lock:
                            self._cache.pop(dev.bus_address, None)
                        if dev.bus_address not in errored:
                            logger.error(
                                "[SmcTempSweeper] %s (slot %s): %s", dev.bus_address, dev.slot_id, e
                            )
                            errored.add(dev.bus_address)
            self._stop_event.wait(_SWEEP_PAUSE_SEC)


def empty_smc_temp() -> dict[str, Any]:
    return {key: None for key, _ in OOB_TEMP_COLUMNS + OOB_PWR_COLUMNS}


def _read_snapshot(slot_id: int) -> dict[str, Any]:
    power_out = commands.read_smc_power_mon(slot_id)
    rails = json.loads(power_out) if power_out.strip() else []
    sensor_out = commands.read_smc_sensor_mon(slot_id)
    sensor = json.loads(sensor_out) if sensor_out.strip() else {}
    snap = empty_smc_temp()
    snap.update(_collect_oob_temps(rails, sensor))
    snap.update(_collect_oob_pwr(rails))
    return snap


def _collect_oob_temps(rails: list[dict], sensor: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in rails:
        key = f"smc_temp_{item.get('device')}"
        if key in _OOB_TEMP_KEYS:
            out[key] = item.get("tdie_c")
    for entry in sensor.get("temp", []):
        key = f"smc_temp_{entry.get('sensor')}"
        if key in _OOB_TEMP_KEYS:
            out[key] = entry.get("temp_c")
    return out


def _collect_oob_pwr(rails: list[dict]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in rails:
        if item.get("device") == "eFUSE":
            out["smc_pwr_eFUSE_volt"] = item.get("volt_mv")
            out["smc_pwr_eFUSE_curr"] = item.get("curr_ma")
    return out


# --- CSV columns -----------------------------------------------------------
OOB_TEMP_COLUMNS: tuple[tuple[str, str], ...] = (
    ("smc_temp_TMP0", "Board Temp1"),
    ("smc_temp_TMP1", "Board Temp2"),
    ("smc_temp_eFUSE", "Efuse Temp"),
    ("smc_temp_CORE0_0P75V", "Core0 Temp"),
    ("smc_temp_CORE1_0P75V", "Core1 Temp"),
    ("smc_temp_HBM_0P85V", "HBM Temp"),
    ("smc_temp_UCIE_VDDQ_0P75V", "UCIe Vddq Temp"),
    ("smc_temp_HBM_VDDQ_1P1V", "HBM Vddq Temp"),
    ("smc_temp_HBM_VDDQL_0P4V(T)", "HBM Vddq-top Temp"),
    ("smc_temp_HBM_VDDQL_0P4V(B)", "HBM Vddq-bottom Temp"),
)

OOB_PWR_COLUMNS: tuple[tuple[str, str], ...] = (
    ("smc_pwr_eFUSE_volt", "eFuse(OOB) voltage"),
    ("smc_pwr_eFUSE_curr", "eFuse(OOB) current"),
)

_OOB_TEMP_KEYS = frozenset(k for k, _ in OOB_TEMP_COLUMNS)

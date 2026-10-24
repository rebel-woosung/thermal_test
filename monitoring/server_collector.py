"""Server-wide BMC sensor collector.

CPU/inlet/system temp + fan RPM via ``ipmitool sdr list all``, server power via
``ipmitool dcmi power reading``.
Output: ``<out_dir>/server_monitoring.csv``
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

import commands
from commands import quiet_shell
from common import CurrentRowTag, append_row, ensure_header, now_iso, to_number
from constants import SERVER_BMC_INTERVAL_SEC

logger = logging.getLogger(__name__)


class ServerMetricCollector(threading.Thread):
    def __init__(
        self,
        current_tag: CurrentRowTag,
        out_dir: Path,
        server_id: str,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name="server-metric", daemon=False)
        self._current_tag = current_tag
        self._server_id = server_id
        self._stop_event = stop_event
        self._csv_path = out_dir / "server_monitoring.csv"
        ensure_header(self._csv_path, SERVER_HEADER)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self.join(timeout=timeout)

    def run(self) -> None:
        logger.info("[ServerMetricCollector] started (%s)", self._current_tag.tag.workload_code)
        error_logged = False
        while not self._stop_event.is_set():
            loop_start = time.monotonic()
            try:
                with quiet_shell():
                    tag = self._current_tag.tag
                    row: dict[str, Any] = {
                        "timestamp": now_iso(),
                        "server_id": self._server_id,
                        "stage_type": tag.stage_type,
                        "workload_code": tag.workload_code,
                        "iteration_index": tag.iteration_index,
                    }
                    row.update(self._collect())
                append_row(self._csv_path, _SERVER_INTERNAL_KEYS, row)
                error_logged = False
            except Exception as e:
                if not error_logged:
                    logger.error("[ServerMetricCollector] %s", e)
                    error_logged = True
            elapsed = time.monotonic() - loop_start
            if self._stop_event.wait(max(0.0, SERVER_BMC_INTERVAL_SEC - elapsed)):
                break

    def _collect(self) -> dict[str, Any]:
        out = commands.read_sensor_records()
        dcmi = commands.read_dcmi_power()
        fields: dict[str, Any] = {}
        for label, key, _ in _TEMP_COLUMNS:
            fields[key] = _parse_temp(out, label)
        for label, key, _ in _FAN_COLUMNS:
            fields[key] = _parse_fan(out, label)
        for label, key, _ in _POWER_COLUMNS:
            fields[key] = _parse_power(dcmi, label)
        return fields


_TEMP_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("CPU1 Temp", "server_cpu1_temp", "CPU1 Temp"),
    ("CPU2 Temp", "server_cpu2_temp", "CPU2 Temp"),
    ("Inlet Temp", "server_inlet_temp", "INLET Temp"),
    ("System Temp", "server_system_temp", "SYSTEM Temp"),
)

_FAN_COLUMNS: tuple[tuple[str, str, str], ...] = tuple(
    (f"FAN{i}", f"server_fan{i}_rpm", f"FAN{i} rpm") for i in range(1, 11)
)

_POWER_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("Instantaneous power reading:", "server_power_instantaneous", "Instantaneous Power[W]"),
    ("Minimum during sampling period:", "server_power_min", "Minimum Power[W]"),
    ("Maximum during sampling period:", "server_power_max", "Maximum Power[W]"),
    ("Average power reading over sample period:", "server_power_avg", "Average Power[W]"),
)

SERVER_HEADER: tuple[str, ...] = (
    "Timestamp",
    "Server ID",
    "Stage Type",
    "workload_code",
    *(h for _, _, h in _TEMP_COLUMNS),
    *(h for _, _, h in _FAN_COLUMNS),
    *(h for _, _, h in _POWER_COLUMNS),
    "iteration_index",
)
_SERVER_INTERNAL_KEYS: tuple[str, ...] = (
    "timestamp",
    "server_id",
    "stage_type",
    "workload_code",
    *(k for _, k, _ in _TEMP_COLUMNS),
    *(k for _, k, _ in _FAN_COLUMNS),
    *(k for _, k, _ in _POWER_COLUMNS),
    "iteration_index",
)


def _parse_temp(output: str, label: str) -> Any:
    pattern = re.compile(rf"^\s*{re.escape(label)}\b.*?(-?\d+(?:\.\d+)?)\s*degrees\s*C", re.IGNORECASE)
    for line in output.splitlines():
        m = pattern.search(line)
        if m:
            return to_number(m.group(1))
    return None


def _parse_fan(output: str, label: str) -> Any:
    pattern = re.compile(rf"^\s*{re.escape(label)}\b.*?(\d+(?:\.\d+)?)\s*RPM", re.IGNORECASE)
    for line in output.splitlines():
        m = pattern.search(line)
        if m:
            return to_number(m.group(1))
    return None


def _parse_power(output: str, label: str) -> Any:
    pattern = re.compile(rf"^\s*{re.escape(label)}\s*(-?\d+(?:\.\d+)?)\s*Watts", re.IGNORECASE)
    for line in output.splitlines():
        m = pattern.search(line)
        if m:
            return to_number(m.group(1))
    return None

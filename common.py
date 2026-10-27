"""Shared building blocks: background worker, CSV/time helpers, rbln parsers."""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# --- device model ----------------------------------------------------------
@dataclass
class Device:
    device_id: int
    bus_address: str
    sid: str
    slot_id: int | None = None

    @property
    def path_safe_bus(self) -> str:
        """Bus address with ``:`` → ``-``, for file and directory names."""
        return self.bus_address.replace(":", "-")


# --- row tag ---------------------------------------------------------------
@dataclass(frozen=True)
class RowTag:
    """Stage Type / workload_code / phase / state / iteration_index stamped on every monitoring row."""

    stage_type: str
    workload_code: str
    iteration_index: int
    phase: str = ""
    state: str = ""


@dataclass
class CurrentRowTag:
    """RowTag in effect now, shared by the session-wide collectors and swapped whole."""

    tag: RowTag


# --- card temp registry ----------------------------------------------------
class CardTempRegistry:
    """Latest Card Temp (rbln-smi ``temperature``) and hottest HBM temp per bus address,
    published by the NPU collectors and read by the fan controller — so the fan reuses the
    monitoring poll instead of issuing its own read."""

    def __init__(self) -> None:
        self._temps: dict[str, float | None] = {}
        self._hbm_temps: dict[str, float | None] = {}
        self._lock = threading.Lock()

    def publish(self, bus_address: str, temp: float | None, hbm_temp: float | None) -> None:
        with self._lock:
            self._temps[bus_address] = temp
            self._hbm_temps[bus_address] = hbm_temp

    def clear(self) -> None:
        with self._lock:
            self._temps.clear()
            self._hbm_temps.clear()

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return {bus: t for bus, t in self._temps.items() if isinstance(t, (int, float))}

    def hottest(self) -> float | None:
        """Highest Card or HBM temp across all cards."""
        with self._lock:
            temps = [*self._temps.values(), *self._hbm_temps.values()]
        return max((t for t in temps if isinstance(t, (int, float))), default=None)


# --- background worker -----------------------------------------------------
class BaseWorker:
    """A thread driven by a shared stop_event; subclasses implement ``run``."""

    def __init__(self, name: str, stop_event: threading.Event) -> None:
        self.name = name
        self.stop_event = stop_event
        self.thread = threading.Thread(name=name, target=self._wrap, daemon=False)

    def start(self) -> None:
        self.thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self.stop_event.set()
        self.thread.join(timeout=timeout)
        if self.thread.is_alive():
            logger.warning("%s did not stop within %.1fs", self.name, timeout)

    def run(self, stop_event: threading.Event) -> None:
        raise NotImplementedError

    def _wrap(self) -> None:
        try:
            self.run(self.stop_event)
        except Exception as e:
            logger.exception("%s crashed: %s", self.name, e)


# --- CSV / time ------------------------------------------------------------
def ensure_header(csv_path: Path, header: tuple[str, ...]) -> None:
    """Write ``header`` once per process per ``csv_path`` (siblings no-op)."""
    with _init_lock:
        if csv_path in _initialized_paths and csv_path.exists():
            return
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write(",".join(header) + "\n")
        _initialized_paths.add(csv_path)


def append_row(csv_path: Path, internal_keys: tuple[str, ...], row: dict[str, Any]) -> None:
    """Append a CSV row in ``internal_keys`` order; missing keys → empty cell."""
    cells = [str(row.get(k, "")) for k in internal_keys]
    with open(csv_path, "a", encoding="utf-8") as f:
        f.write(",".join(cells) + "\n")


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


# --- rbln output parsers ---------------------------------------------------
def to_number(s: str) -> Any:
    try:
        return float(s) if "." in s else int(s)
    except ValueError:
        return s


def strip_unit(value: Any) -> Any:
    if value is None:
        return None
    m = _LEADING_NUMBER_RE.match(str(value).strip())
    return to_number(m.group(0)) if m else value


def parse_kv(output: str, key: str) -> Any:
    """`Key : value` line parser used by sysinfo / profile output."""
    for line in output.splitlines():
        if key in line:
            parts = line.split(":")
            if len(parts) >= 2:
                return to_number(parts[-1].strip())
    return None


def parse_hbm_cl_temps(output: str) -> dict[int, Any]:
    """chiplet_id → DRAM Temperature (C) from "Chiplet N:" blocks in sysinfo thermal."""
    return {int(m.group(1)): to_number(m.group(2)) for m in _HBM_CL_RE.finditer(output)}


def parse_smc(output: str, key: str) -> Any:
    """SMC ``key [=:] value`` parser, key anchored at line start with a word boundary."""
    pattern = re.compile(rf"^\s*{re.escape(key)}\b.*?[=:]\s*(-?[\d.]+)", re.IGNORECASE)
    for line in output.splitlines():
        m = pattern.search(line)
        if m:
            return to_number(m.group(1))
    return None


def parse_profile(output: str) -> tuple[Any, Any, Any]:
    """Return ``(ber, ecc, data)`` from the profile trailer line; ``None`` each if absent."""
    return (
        _first_number(_BER_RE, output),
        _first_number(_ECC_RE, output),
        _first_number(_DATA_RE, output),
    )


def parse_chiplet_util(output: str) -> dict[int, Any]:
    """chiplet_id → utilization (%) from ``Utilization Chiplet IDN`` lines in sysinfo util."""
    return {int(m.group(1)): to_number(m.group(2)) for m in _UTIL_CL_RE.finditer(output)}


def parse_ucie(output: str) -> dict[int, dict[str, Any]]:
    """chiplet_id → ``{"crc", "width", "speed"}`` from the UCIe chiplet header (plus ``"error"`` on mismatch)."""
    result: dict[int, dict[str, Any]] = {}
    for m in _UCIE_CHIPLET_HDR_RE.finditer(output):
        chiplet_id = int(m.group(1))
        header = m.group(2)

        crc_m = _UCIE_CRC_ERR_RE.search(header)
        crc = int(crc_m.group(1)) if crc_m else None

        mismatch = _UCIE_MISMATCH_RE.search(header)
        if mismatch:
            result[chiplet_id] = {
                "crc": crc,
                "width": _UCIE_MISMATCH_SENTINEL,
                "speed": _UCIE_MISMATCH_SENTINEL,
                "error": mismatch.group(0).strip(),
            }
            continue

        width = _UCIE_WIDTH_RE.search(header)
        speed = _UCIE_SPEED_RE.search(header)
        result[chiplet_id] = {
            "crc": crc,
            "width": int(width.group(1)) if width else None,
            "speed": int(speed.group(1)) if speed else None,
        }
    return result


# --- module-private state / helpers ----------------------------------------
_initialized_paths: set[Path] = set()
_init_lock = threading.Lock()

_LEADING_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_HBM_CL_RE = re.compile(r"Chiplet\s+(\d+):\s*\n\s*DRAM Temperature \(C\)\s*:\s*([\d.]+)")
_BER_RE = re.compile(r"\bber\s*=\s*([\d.eE+-]+)")
_ECC_RE = re.compile(r"\becc\(([\d.eE+-]+)\)")
_DATA_RE = re.compile(r"\bdata\(([\d.eE+-]+)\)")
_UTIL_CL_RE = re.compile(r"Utilization Chiplet ID(\d+)\s*:\s*([\d.]+)")
_UCIE_CHIPLET_HDR_RE = re.compile(r"Chiplet\s+(\d+)\s*\|([^\n]*)")
_UCIE_CRC_ERR_RE = re.compile(r"crc_err:\s*(\d+)")
_UCIE_WIDTH_RE = re.compile(r"width=x?(\d+)")
_UCIE_SPEED_RE = re.compile(r"speed=(\d+)")
_UCIE_MISMATCH_RE = re.compile(r"ERROR[^\n]*", re.IGNORECASE)
_UCIE_MISMATCH_SENTINEL = -1


def _first_number(pattern: re.Pattern[str], output: str) -> Any:
    m = pattern.search(output)
    return to_number(m.group(1)) if m else None

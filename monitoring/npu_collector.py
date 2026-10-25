"""Per-device NPU monitoring — CP + SMC into one CSV row.

Output: ``<out_dir>/npu_monitoring_<bus>.csv`` — one file per device.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

import commands
from commands import quiet_shell
from common import (
    CardTempRegistry,
    CurrentRowTag,
    Device,
    append_row,
    ensure_header,
    now_iso,
    parse_chiplet_util,
    parse_hbm_cl_temps,
    parse_kv,
    parse_profile,
    parse_smc,
    parse_ucie,
    strip_unit,
)
from constants import NPU_INTERVAL_SEC

logger = logging.getLogger(__name__)

_CHIPLET_COUNT = 4
_STOP_TIMEOUT_SEC = 30.0


class NpuMonitor:
    """Per-device NpuMetricCollectors, stopped around hw_cfg so no read hits a device mid-reset."""

    def __init__(
        self,
        current_tag: CurrentRowTag,
        card_temps: CardTempRegistry,
        out_dir: Path,
        server_id: str,
    ) -> None:
        self._current_tag = current_tag
        self._card_temps = card_temps
        self._out_dir = out_dir
        self._server_id = server_id
        self._collectors: list[NpuMetricCollector] = []

    def start(self, devices: list[Device]) -> None:
        stop_event = threading.Event()
        self._collectors = [
            NpuMetricCollector(
                dev,
                self._current_tag,
                self._card_temps,
                self._out_dir,
                self._server_id,
                stop_event,
            )
            for dev in devices
        ]
        for c in self._collectors:
            c.start()

    def stop(self) -> None:
        for c in self._collectors:
            c.stop(timeout=_STOP_TIMEOUT_SEC)
            if c.is_alive():
                logger.warning("[NpuMonitor] %s still running after %.0fs", c.name, _STOP_TIMEOUT_SEC)
        self._collectors = []
        self._card_temps.clear()


class NpuMetricCollector(threading.Thread):
    def __init__(
        self,
        device: Device,
        current_tag: CurrentRowTag,
        card_temps: CardTempRegistry,
        out_dir: Path,
        server_id: str,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name=f"npu-metric-{device.device_id}", daemon=False)
        self._device = device
        self._current_tag = current_tag
        self._card_temps = card_temps
        self._server_id = server_id
        self._stop_event = stop_event
        self._ucie_warned = False
        self._csv_path = out_dir / f"npu_monitoring_{device.path_safe_bus}.csv"
        ensure_header(self._csv_path, NPU_HEADER)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self.join(timeout=timeout)

    def run(self) -> None:
        logger.info(
            "[NpuMetricCollector] rbln%d started (%s)",
            self._device.device_id,
            self._current_tag.tag.workload_code,
        )
        error_logged = False
        while not self._stop_event.is_set():
            loop_start = time.monotonic()
            try:
                with quiet_shell():
                    tag = self._current_tag.tag
                    row: dict[str, Any] = {
                        "timestamp": now_iso(),
                        "server_id": self._server_id,
                        "card_serial_num": self._device.sid,
                        "bus_address": self._device.bus_address,
                        "stage_type": tag.stage_type,
                        "workload_code": tag.workload_code,
                        "phase": tag.phase,
                        "iteration_index": tag.iteration_index,
                    }
                    for collect in (
                        self._collect_smi,
                        self._collect_thermal,
                        self._collect_util,
                        self._collect_ucie,
                        self._collect_profile,
                        self._collect_smc_pwr,
                    ):
                        if self._stop_event.is_set():
                            return
                        row.update(collect())
                append_row(self._csv_path, _NPU_INTERNAL_KEYS, row)
                self._card_temps.publish(
                    self._device.bus_address, row.get("temperature"), _hottest_hbm(row)
                )
                error_logged = False
            except Exception as e:
                if not error_logged:
                    logger.error("[NpuMetricCollector] rbln%d: %s", self._device.device_id, e)
                    error_logged = True
            elapsed = time.monotonic() - loop_start
            if self._stop_event.wait(max(0.0, NPU_INTERVAL_SEC - elapsed)):
                break

    def _collect_smi(self) -> dict[str, Any]:
        snap = commands.get_device_snapshot(self._device.device_id)
        dev = (snap.get("devices") or [{}])[0]
        pci = dev.get("pci", {})
        return {
            "link_speed": strip_unit(pci.get("link_speed")),
            "link_width": pci.get("link_width"),
            "util": dev.get("util"),
            "card_power": strip_unit(dev.get("card_power")),
            "temperature": strip_unit(dev.get("temperature")),
            "pstate": dev.get("pstate"),
        }

    def _collect_thermal(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        soc_out = commands.read_thermal(self._device.device_id)
        out["soc_peak_temp"] = parse_kv(soc_out, "Temperature (C)")
        cl_temps = parse_hbm_cl_temps(soc_out)
        for chip in range(_CHIPLET_COUNT):
            out[f"hbm_cl{chip}_temp"] = cl_temps.get(chip)
            chip_out = commands.read_thermal(self._device.device_id, chiplet_id=chip)
            out[f"chiplet{chip}_temp"] = parse_kv(chip_out, "Temperature (C)")
        return out

    def _collect_util(self) -> dict[str, Any]:
        cl_util = parse_chiplet_util(commands.read_util(self._device.device_id))
        return {f"cl{chip}_util": cl_util.get(chip) for chip in range(_CHIPLET_COUNT)}

    def _collect_ucie(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        error = None
        for chip in range(_CHIPLET_COUNT):
            info = parse_ucie(commands.read_ucie(self._device.device_id, chip)).get(chip, {})
            error = error or info.get("error")
            result[f"cl{chip}_ucie_crc"] = info.get("crc")
            result[f"cl{chip}_ucie_width"] = info.get("width")
            result[f"cl{chip}_ucie_speed"] = info.get("speed")
        if error and not self._ucie_warned:
            logger.warning("[NpuMetricCollector] rbln%d UCIe: %s", self._device.device_id, error)
        self._ucie_warned = bool(error)
        return result

    def _collect_profile(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for chip in range(_CHIPLET_COUNT):
            ber, ecc, data = parse_profile(commands.read_profile(self._device.device_id, chip))
            result[f"hbm_cl{chip}_ber"] = ber
            result[f"hbm_cl{chip}_ecc"] = ecc
            result[f"hbm_cl{chip}_data"] = data
        return result

    def _collect_smc_pwr(self) -> dict[str, Any]:
        out = commands.read_smc_pwr(self._device.device_id)
        return {k: parse_smc(out, k) for k, _ in _SMC_PWR_COLUMNS}


def _hottest_hbm(row: dict[str, Any]) -> float | None:
    temps = [row.get(f"hbm_cl{chip}_temp") for chip in range(_CHIPLET_COUNT)]
    return max((t for t in temps if isinstance(t, (int, float))), default=None)


# --- CSV columns -----------------------------------------------------------
_CP_COLUMNS: tuple[tuple[str, str], ...] = (
    ("link_speed", "PCIe Link Speed[GT/s]"),
    ("link_width", "PCIe Link Width"),
    ("util", "Utilization of DNC"),
    ("card_power", "Card Power[uW]"),
    ("temperature", "Card Temp"),
    ("pstate", "Power state"),
    ("soc_peak_temp", "SoC Peak (Card Temp)"),
    ("chiplet0_temp", "Chiplet.0 Peak temp"),
    ("chiplet1_temp", "Chiplet.1 Peak temp"),
    ("chiplet2_temp", "Chiplet.2 Peak temp"),
    ("chiplet3_temp", "Chiplet.3 Peak temp"),
    ("hbm_cl0_temp", "HBM-CL0 temp"),
    ("hbm_cl1_temp", "HBM-CL1 temp"),
    ("hbm_cl2_temp", "HBM-CL2 temp"),
    ("hbm_cl3_temp", "HBM-CL3 temp"),
    ("cl0_util", "CL0 Utilization"),
    ("cl1_util", "CL1 Utilization"),
    ("cl2_util", "CL2 Utilization"),
    ("cl3_util", "CL3 Utilization"),
    ("cl0_ucie_crc", "CL0 UCIe CRC"),
    ("cl0_ucie_width", "CL0 UCIe Width"),
    ("cl0_ucie_speed", "CL0 UCIe Speed"),
    ("cl1_ucie_crc", "CL1 UCIe CRC"),
    ("cl1_ucie_width", "CL1 UCIe Width"),
    ("cl1_ucie_speed", "CL1 UCIe Speed"),
    ("cl2_ucie_crc", "CL2 UCIe CRC"),
    ("cl2_ucie_width", "CL2 UCIe Width"),
    ("cl2_ucie_speed", "CL2 UCIe Speed"),
    ("cl3_ucie_crc", "CL3 UCIe CRC"),
    ("cl3_ucie_width", "CL3 UCIe Width"),
    ("cl3_ucie_speed", "CL3 UCIe Speed"),
    ("hbm_cl0_ber", "HBM CL0 BER"),
    ("hbm_cl0_ecc", "HBM CL0 ECC"),
    ("hbm_cl0_data", "HBM CL0 Data"),
    ("hbm_cl1_ber", "HBM CL1 BER"),
    ("hbm_cl1_ecc", "HBM CL1 ECC"),
    ("hbm_cl1_data", "HBM CL1 Data"),
    ("hbm_cl2_ber", "HBM CL2 BER"),
    ("hbm_cl2_ecc", "HBM CL2 ECC"),
    ("hbm_cl2_data", "HBM CL2 Data"),
    ("hbm_cl3_ber", "HBM CL3 BER"),
    ("hbm_cl3_ecc", "HBM CL3 ECC"),
    ("hbm_cl3_data", "HBM CL3 Data"),
)

_SMC_PWR_COLUMNS: tuple[tuple[str, str], ...] = (
    ("efuse_voltage", "eFuse(Input) voltage"),
    ("efuse_current", "eFuse(Input) current"),
    ("rebel_core0_voltage", "REBEL-Q Core0 voltage"),
    ("rebel_core0_current", "REBEL-Q Core0 current"),
    ("rebel_core1_voltage", "REBEL-Q Core1 voltage"),
    ("rebel_core1_current", "REBEL-Q Core1 current"),
    ("rebel_hbm_voltage", "HBM voltage"),
    ("rebel_hbm_current", "HBM current"),
    ("rebel_hbm_vpp_voltage", "HBM VPP voltage"),
    ("rebel_hbm_vpp_current", "HBM VPP current"),
    ("hbm_vddq_voltage", "HBM VDDQ voltage"),
    ("hbm_vddq_current", "HBM VDDQ current"),
    ("ucie_vddq_voltage", "UCIE VDDQ voltage"),
    ("ucie_vddq_current", "UCIE VDDQ current"),
    ("pcie_vp_voltage", "PCIe VP voltage"),
    ("pcie_vp_current", "PCIe VP current"),
    ("avdd_1p2v_voltage", "AVDD 1.2V voltage"),
    ("avdd_1p2v_current", "AVDD 1.2V current"),
    ("avdd_3p3v_voltage", "AVDD 3.3V voltage"),
    ("avdd_3p3v_current", "AVDD 3.3V current"),
    ("hbm_vddql_top_voltage", "HBM VDDQL Top voltage"),
    ("hbm_vddql_top_current", "HBM VDDQL Top current"),
    ("hbm_vddql_btm_voltage", "HBM VDDQL Bottom voltage"),
    ("hbm_vddql_btm_current", "HBM VDDQL Bottom current"),
    ("pci1_vp_voltage", "PCI1 VP voltage"),
    ("pci1_vp_current", "PCI1 VP current"),
    ("sys_5p0v_voltage", "SYS 5.0v voltage"),
    ("sys_5p0v_current", "SYS 5.0v current"),
    ("ldo_3p3v_voltage", "LDO 3.3v voltage"),
    ("ldo_3p3v_current", "LDO 3.3v current"),
)

_NPU_COLUMNS = _CP_COLUMNS + _SMC_PWR_COLUMNS

NPU_HEADER: tuple[str, ...] = (
    "Serial Number",
    "Bus Address",
    "Timestamp",
    "Server ID",
    "Stage Type",
    "workload_code",
    "phase",
    *(h for _, h in _NPU_COLUMNS),
    "iteration_index",
)
_NPU_INTERNAL_KEYS: tuple[str, ...] = (
    "card_serial_num",
    "bus_address",
    "timestamp",
    "server_id",
    "stage_type",
    "workload_code",
    "phase",
    *(k for k, _ in _NPU_COLUMNS),
    "iteration_index",
)

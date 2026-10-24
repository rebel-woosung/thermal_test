"""HBM CL0 thermal test — run the workloads/CR03.json stages while a fan controller
steps the BMC fan duty by HBM CL0 temperature, collecting NPU/server/SMC metrics.

Run (foreground):  cd thermal_test && uv run python main.py
"""

from __future__ import annotations

import getpass
import json
import logging
import re
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import commands
from commands import CommandFailedError
from common import CurrentRowTag, Device, HbmTempRegistry, RowTag
from constants import (
    FAN_ZONES,
    LOG_DIR,
    REST_SEC,
    RESTORE_FAN_DUTY_ON_EXIT,
    WORKLOAD_DIR,
    WORKLOADS_JSON,
)
from fan_controller import FanController
from monitoring.npu_collector import NpuMonitor
from monitoring.server_collector import ServerMetricCollector
from monitoring.smc_sweeper import SmcTempSweeper

logger = logging.getLogger(__name__)

_NOPASSWD_SUDOERS = "/etc/sudoers.d/99-thermal-test-nopasswd"
_HWCFG_LABEL = "HW_CFG"
_HARD_RESET_LABEL = "HARD_RESET"
_REST_LABEL = "REST"
_STAGES: tuple[tuple[str, str], ...] = (
    ("iteration", "Iteration"),
    ("prestress", "Pre-stress"),
    ("enhanced_stress", "Enhanced"),
    ("baseline_stress", "Baseline"),
    ("poststress", "Post-stress"),
)

_HWCFG_VERIFY_ATTEMPTS = 3
_POST_RESET_DELAY_SEC = 5
_CORE_VOLTAGE_TOLERANCE_MV = 5
_RESET_RETRY_DELAY_SEC = 3
_NORMAL_TIMEOUT_SEC = 300
_NORMAL_POLL_SEC = 30
_FW_INIT_TIMEOUT_SEC = 30
_FW_INIT_POLL_SEC = 5


class HwCfgError(Exception):
    """hw_cfg verify failed after a workload's write + hard reset — abort the run."""


class HardResetError(Exception):
    """A device did not come back from hard reset — abort the run."""


def main() -> None:
    run_dir = LOG_DIR / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    _setup_logging(run_dir / "server.log")
    signal.signal(signal.SIGINT, _handle_sigint)
    sudo_added = _ensure_passwordless_sudo()
    try:
        _run_session(run_dir)
    finally:
        if sudo_added:
            _remove_passwordless_sudo()


def _run_session(run_dir: Path) -> None:
    server_id = socket.gethostname()
    runs = _load_runs()
    if not runs:
        logger.error("no workloads in %s — aborting", WORKLOADS_JSON)
        return
    work_sec = sum(wl["test_time_sec"] for _, wl in runs)
    logger.info(
        "%d workload runs from %s — %.1f h workload + %.1f h rest",
        len(runs),
        WORKLOADS_JSON.name,
        work_sec / 3600,
        (len(runs) - 1) * REST_SEC / 3600,
    )
    devices = _discover_devices()
    if not devices:
        logger.error("no devices found via rbln-smi — aborting")
        return

    session_stop = threading.Event()
    hbm_registry = HbmTempRegistry()
    current_tag = CurrentRowTag(replace(runs[0][0], workload_code=_HWCFG_LABEL))
    smc = SmcTempSweeper(devices, session_stop)
    fan = FanController(hbm_registry, run_dir, session_stop)
    server = ServerMetricCollector(current_tag, run_dir, server_id, session_stop)
    npu = NpuMonitor(devices, current_tag, smc, hbm_registry, run_dir, server_id)
    smc.start()
    fan.start()
    server.start()

    try:
        for n, (tag, wl) in enumerate(runs, start=1):
            logger.info(
                "=== [%d/%d] %s #%d — workload %s (%s) — %d min ===",
                n,
                len(runs),
                tag.stage_type,
                tag.iteration_index,
                tag.workload_code,
                wl["workload_type"],
                wl["test_time_sec"] // 60,
            )
            _run_workload(tag, wl, devices, npu, current_tag, run_dir)
            if n < len(runs):
                current_tag.tag = replace(tag, workload_code=_REST_LABEL)
                npu.start()
                logger.info("resting %d min before next workload", REST_SEC // 60)
                if session_stop.wait(REST_SEC):
                    break
    except KeyboardInterrupt:
        logger.warning("interrupted — shutting down")
    finally:
        commands.terminate_all_subprocesses()
        npu.stop()
        server.stop()
        fan.stop()
        smc.stop()
        if RESTORE_FAN_DUTY_ON_EXIT is not None:
            logger.info("restoring fan duty to %d%%", RESTORE_FAN_DUTY_ON_EXIT)
            try:
                commands.set_fan_duty_all(RESTORE_FAN_DUTY_ON_EXIT, FAN_ZONES)
            except Exception as e:
                logger.error("fan restore failed: %s", e)
        logger.info("done — output in %s", run_dir)


# --- module-private helpers ------------------------------------------------
def _load_runs() -> list[tuple[RowTag, dict]]:
    """Every workload run in engine stage order; the iteration stage repeats ``repeat_count`` times."""
    with open(WORKLOADS_JSON, encoding="utf-8") as f:
        config = json.load(f)
    unknown = set(config) - {key for key, _ in _STAGES}
    if unknown:
        logger.warning("%s: ignoring unknown stage(s) %s", WORKLOADS_JSON.name, sorted(unknown))
    runs: list[tuple[RowTag, dict]] = []
    for key, stage_type in _STAGES:
        stage = config.get(key)
        if stage is None:
            continue
        for i in range(1, stage.get("repeat_count", 1) + 1):
            runs.extend((RowTag(stage_type, wl["code"], i), wl) for wl in stage["workloads"])
    return runs


def _discover_devices() -> list[Device]:
    raw = commands.list_device_snapshots()
    devices: list[Device] = []
    for dev in raw.get("devices", []):
        device_str = dev.get("device", "").removeprefix("rbln")
        bus = dev.get("pci", {}).get("bus_id", "")
        sid = dev.get("sid", "")
        if not device_str.isdigit() or not bus:
            logger.warning("skip malformed rbln-smi entry: %s", dev)
            continue
        device_id = int(device_str)
        slot_id = _slot_for(bus, device_id)
        devices.append(Device(device_id=device_id, slot_id=slot_id, bus_address=bus, sid=sid[-8:]))
    devices.sort(key=lambda d: d.slot_id)
    for d in devices:
        logger.info("device rbln%d slot=%d bus=%s sid=%s", d.device_id, d.slot_id, d.bus_address, d.sid)
    return devices


def _slot_for(bus_address: str, device_id: int) -> int:
    out = commands.dmidecode_slot_for(bus_address).strip()
    for token in out.split():
        if token.isdigit():
            return int(token)
    logger.warning("no dmidecode slot for %s — using device_id %d as slot", bus_address, device_id)
    return device_id


def _apply_hwcfg(devices: list[Device], hwcfg: dict) -> None:
    """Engine UpdateHwCfgStep: write once, then (hard reset → 5s → verify) up to 3 times."""
    logger.info("applying hw_cfg %s to %d device(s)", hwcfg, len(devices))
    _write_hwcfg(devices, hwcfg)
    for attempt in range(1, _HWCFG_VERIFY_ATTEMPTS + 1):
        _hard_reset(devices)
        logger.info("delay %ds", _POST_RESET_DELAY_SEC)
        time.sleep(_POST_RESET_DELAY_SEC)
        unverified = _unverified_devices(devices, hwcfg)
        if not unverified:
            logger.info("hw_cfg applied + verified on all devices")
            return
        logger.warning(
            "hw_cfg verify FAIL on attempt %d/%d (slots %s)",
            attempt,
            _HWCFG_VERIFY_ATTEMPTS,
            [d.slot_id for d in unverified],
        )
    raise HwCfgError(
        f"hw_cfg verify failed after {_HWCFG_VERIFY_ATTEMPTS} attempts: "
        f"slots {[d.slot_id for d in unverified]}"
    )


def _write_hwcfg(devices: list[Device], hwcfg: dict) -> None:
    for d in devices:
        if commands.read_hw_cfg(d.device_id) == hwcfg:
            logger.info("slot %d: hw_cfg matches target, skip write", d.slot_id)
            continue
        commands.write_hw_cfg(
            d.device_id, hwcfg["dcluster_clk"], hwcfg["hbm3_data_rate"], hwcfg["dnc_core_vol"]
        )
        applied = commands.read_hw_cfg(d.device_id)
        if applied != hwcfg:
            raise HwCfgError(f"slot {d.slot_id} (rbln{d.device_id}): applied hw_cfg {applied} != {hwcfg}")
        logger.info("slot %d: hw_cfg written → %s", d.slot_id, hwcfg)


def _hard_reset(devices: list[Device]) -> None:
    """Engine ExecuteHardResetAction: reset all in parallel, wait status=normal, then FW init."""
    with ThreadPoolExecutor(max_workers=len(devices)) as pool:
        for future in as_completed([pool.submit(_trigger_hard_reset, d) for d in devices]):
            future.result()
    _wait_for(
        devices,
        lambda s: s.get("status") == "normal",
        "status=normal",
        _NORMAL_TIMEOUT_SEC,
        _NORMAL_POLL_SEC,
    )
    _wait_for(
        devices,
        lambda s: _num(s.get("temperature")) > 0 and _num(s.get("card_power")) > 0,
        "FW initialized (temp/power > 0)",
        _FW_INIT_TIMEOUT_SEC,
        _FW_INIT_POLL_SEC,
    )
    _refresh_device_ids(devices)


def _trigger_hard_reset(device: Device) -> None:
    try:
        commands.hard_reset(device.device_id)
    except Exception as e:
        logger.warning(
            "slot %d: hard reset trigger failed: %s; retrying in %ds",
            device.slot_id,
            e,
            _RESET_RETRY_DELAY_SEC,
        )
        time.sleep(_RESET_RETRY_DELAY_SEC)
        commands.hard_reset(device.device_id)
    logger.info("slot %d: hard reset triggered", device.slot_id)


def _wait_for(
    devices: list[Device],
    is_ready: Callable[[dict], bool],
    what: str,
    timeout_sec: int,
    poll_sec: int,
) -> None:
    deadline = time.monotonic() + timeout_sec
    while True:
        by_bus = _snapshot_by_bus()
        pending = [d for d in devices if not is_ready(by_bus.get(d.bus_address, {}))]
        if not pending:
            logger.info("all devices %s", what)
            return
        if time.monotonic() >= deadline:
            raise HardResetError(
                f"slots {[d.slot_id for d in pending]} not {what} {timeout_sec}s after hard reset"
            )
        logger.info("waiting for %s (%d pending, sleep %ds)", what, len(pending), poll_sec)
        time.sleep(poll_sec)


def _refresh_device_ids(devices: list[Device]) -> None:
    by_bus = _snapshot_by_bus()
    for d in devices:
        name = by_bus.get(d.bus_address, {}).get("device", "").removeprefix("rbln")
        if name.isdigit() and int(name) != d.device_id:
            logger.warning("slot %d: rbln%d → rbln%s after hard reset", d.slot_id, d.device_id, name)
            d.device_id = int(name)


def _snapshot_by_bus() -> dict[str, dict]:
    return {
        dev.get("pci", {}).get("bus_id", ""): dev
        for dev in commands.list_device_snapshots().get("devices", [])
    }


def _unverified_devices(devices: list[Device], hwcfg: dict) -> list[Device]:
    unverified: list[Device] = []
    for d in devices:
        problem = _hwcfg_mismatch(d, hwcfg)
        if problem:
            logger.warning("slot %d (rbln%d): %s", d.slot_id, d.device_id, problem)
            unverified.append(d)
        else:
            logger.info("slot %d: hw_cfg verified", d.slot_id)
    return unverified


def _hwcfg_mismatch(device: Device, hwcfg: dict) -> str | None:
    cfg = commands.read_hw_cfg(device.device_id)
    freq = commands.read_cmu_freq(device.device_id)
    volt = commands.read_core_voltage_mv(device.device_id)
    if cfg != hwcfg:
        return f"hw_cfg readback {cfg} != {hwcfg}"
    if freq.get("DRAM") != hwcfg["hbm3_data_rate"]:
        return f"DRAM freq {freq.get('DRAM')} != {hwcfg['hbm3_data_rate']}"
    if freq.get("DCL0") != hwcfg["dcluster_clk"]:
        return f"DCL0 freq {freq.get('DCL0')} != {hwcfg['dcluster_clk']}"
    if volt is None or abs(volt - hwcfg["dnc_core_vol"]) > _CORE_VOLTAGE_TOLERANCE_MV:
        return f"core voltage {volt}mV != {hwcfg['dnc_core_vol']}mV (±{_CORE_VOLTAGE_TOLERANCE_MV}mV)"
    return None


def _num(value: object) -> float:
    if isinstance(value, (int, float)):
        return value
    m = re.search(r"-?\d+", str(value))
    return int(m.group()) if m else 0


def _run_workload(
    tag: RowTag,
    wl: dict,
    devices: list[Device],
    npu: NpuMonitor,
    current_tag: CurrentRowTag,
    run_dir: Path,
) -> None:
    npu.stop()
    current_tag.tag = replace(tag, workload_code=_HWCFG_LABEL)
    _apply_hwcfg(devices, wl["hwcfg"])
    current_tag.tag = tag
    npu.start()

    with ThreadPoolExecutor(max_workers=len(devices)) as executor:
        futures = [executor.submit(_retrace_device, dev, wl, run_dir) for dev in devices]
        for future in as_completed(futures):
            future.result()

    npu.stop()
    current_tag.tag = replace(tag, workload_code=_HARD_RESET_LABEL)
    logger.info("workload done — hard reset")
    _hard_reset(devices)


def _retrace_device(device: Device, wl: dict, run_dir: Path) -> None:
    paths = [str(WORKLOAD_DIR / f) for f in wl["filename"]]
    test_time_sec = wl["test_time_sec"]

    if wl["workload_type"] == "DTC_SINGLE":
        with ThreadPoolExecutor(max_workers=len(paths)) as pool:
            futures = [
                pool.submit(_retrace_one, device, p, run_dir, wl["code"], test_time_sec, idx)
                for idx, p in enumerate(paths)
            ]
            for future in as_completed(futures):
                future.result()
    else:
        _retrace_one(device, paths[0], run_dir, wl["code"], test_time_sec, None)


def _retrace_one(
    device: Device,
    path: str,
    run_dir: Path,
    code: str,
    test_time_sec: int,
    chiplet_idx: int | None,
) -> None:
    suffix = f"_CL{chiplet_idx}" if chiplet_idx is not None else ""
    try:
        commands.retrace_workload(
            device_id=device.device_id,
            workload_path=path,
            test_time_sec=test_time_sec,
            log_dir=str(run_dir),
        )
    except subprocess.TimeoutExpired as e:
        logger.error("rbln%d %s%s: retrace timed out: %s", device.device_id, code, suffix, e)
    except CommandFailedError as e:
        logger.error("rbln%d %s%s: retrace failed: %s", device.device_id, code, suffix, e)
    except Exception as e:
        logger.error("rbln%d %s%s: retrace error: %s", device.device_id, code, suffix, e)


def _handle_sigint(signum: int, frame: object) -> None:
    logger.warning("interrupted (SIGINT) — killing running subprocesses")
    commands.terminate_all_subprocesses()
    raise KeyboardInterrupt


def _ensure_passwordless_sudo() -> bool:
    """thermal_test runs device commands via ``sudo bash -lc ...`` and assumes
    NOPASSWD sudo (as on the real SRT host). On a PC without it, grant passwordless
    sudo to the current user for this run (prompts for the password once). Returns
    True if this call added the drop-in — the caller removes it on exit."""
    # -k clears cached creds so the check detects genuine NOPASSWD (device commands
    # run tty-less, where a cached timestamp doesn't apply).
    subprocess.run(["sudo", "-k"], capture_output=True)
    if subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0:
        return False
    user = getpass.getuser()
    logger.warning(
        "passwordless sudo not set — configuring %s (enter your password once)", _NOPASSWD_SUDOERS
    )
    rule = f"{user} ALL=(ALL) NOPASSWD:ALL\n"
    subprocess.run(
        ["sudo", "tee", _NOPASSWD_SUDOERS],
        input=rule,
        text=True,
        stdout=subprocess.DEVNULL,
        check=True,
    )
    subprocess.run(["sudo", "chmod", "440", _NOPASSWD_SUDOERS], check=True)
    if subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode != 0:
        raise RuntimeError("failed to configure passwordless sudo")
    logger.info("passwordless sudo configured (auto-removed on exit)")
    return True


def _remove_passwordless_sudo() -> None:
    logger.info("removing passwordless sudo (%s)", _NOPASSWD_SUDOERS)
    result = subprocess.run(
        ["sudo", "rm", "-f", _NOPASSWD_SUDOERS], capture_output=True, text=True
    )
    if result.returncode != 0:
        logger.error(
            "failed to remove %s — remove manually: %s", _NOPASSWD_SUDOERS, result.stderr.strip()
        )


def _setup_logging(log_file: Path) -> None:
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[logging.StreamHandler(), logging.FileHandler(log_file, encoding="utf-8")],
    )


if __name__ == "__main__":
    main()

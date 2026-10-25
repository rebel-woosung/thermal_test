"""Card thermal test — run each workloads/CR03.json workload through four fan phases
(MAX → TARGET → duty sweep down → duty sweep up, see test_config.py), with a hard reset
before every segment, collecting NPU/server/SMC metrics.

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
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

import commands
from commands import CommandFailedError
from common import CardTempRegistry, CurrentRowTag, Device, RowTag
from constants import (
    FAN_ZONES,
    LOG_DIR,
    RESTORE_FAN_DUTY_ON_EXIT,
    WORKLOAD_DIR,
    WORKLOADS_JSON,
)
from fan_controller import FanController
from monitoring.npu_collector import NpuMonitor
from monitoring.server_collector import ServerMetricCollector
from test_config import (
    FAN_TARGET_RISE_C,
    MAX_PHASE_SEC,
    REST_SEC,
    SWEEP_DUTIES,
    SWEEP_SAFETY_TEMP_C,
    SWEEP_STEP_SEC,
    TARGET_PHASE_SEC,
)

logger = logging.getLogger(__name__)

_NOPASSWD_SUDOERS = "/etc/sudoers.d/99-thermal-test-nopasswd"
_HWCFG_LABEL = "HW_CFG"
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


_MAX_PHASE = "MAX"
_TARGET_PHASE = "TARGET"
_DOWN_PHASE = "DOWN"
_UP_PHASE = "UP"


class HwCfgError(Exception):
    """hw_cfg verify failed after a workload's write + hard reset — abort the run."""


@dataclass(frozen=True)
class _Segment:
    """One retrace run of a workload; a ``starts_phase`` segment is preceded by REST."""

    index: int
    kind: str
    phase: str
    duration_sec: int
    duty: int | None
    starts_phase: bool


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
    segments = _segments()
    steps = [(replace(tag, phase=seg.phase), wl, seg) for tag, wl in runs for seg in segments]
    work_sec = len(runs) * sum(seg.duration_sec for seg in segments)
    rest_count = sum(seg.starts_phase for _, _, seg in steps) - 1
    logger.info(
        "%d workloads × %d segments = %d runs from %s — %.1f h workload + %.1f h rest"
        " + a hard reset per run",
        len(runs),
        len(segments),
        len(steps),
        WORKLOADS_JSON.name,
        work_sec / 3600,
        rest_count * REST_SEC / 3600,
    )
    logger.info(
        "test_config: MAX_PHASE_SEC=%s, TARGET_PHASE_SEC=%s, SWEEP_STEP_SEC=%s, SWEEP_DUTIES=%s,"
        " FAN_TARGET_RISE_C=%s, SWEEP_SAFETY_TEMP_C=%s, REST_SEC=%s",
        MAX_PHASE_SEC,
        TARGET_PHASE_SEC,
        SWEEP_STEP_SEC,
        SWEEP_DUTIES,
        FAN_TARGET_RISE_C,
        SWEEP_SAFETY_TEMP_C,
        REST_SEC,
    )
    devices = _discover_devices()
    if not devices:
        logger.error("no devices found via rbln-smi — aborting")
        return

    session_stop = threading.Event()
    card_temps = CardTempRegistry()
    current_tag = CurrentRowTag(replace(steps[0][0], workload_code=_HWCFG_LABEL))
    fan = FanController(card_temps, current_tag, run_dir, session_stop)
    server = ServerMetricCollector(current_tag, run_dir, server_id, session_stop)
    npu = NpuMonitor(current_tag, card_temps, run_dir, server_id)
    fan.start()
    server.start()

    try:
        prev_tag: RowTag | None = None
        for n, (tag, wl, seg) in enumerate(steps, start=1):
            if prev_tag is not None and seg.starts_phase:
                current_tag.tag = replace(prev_tag, workload_code=_REST_LABEL)
                logger.info("resting %d s before %s", REST_SEC, seg.phase)
                if session_stop.wait(REST_SEC):
                    break
            logger.info(
                "=== [%d/%d] %s #%d — workload %s (%s) — %s — %d s ===",
                n,
                len(steps),
                tag.stage_type,
                tag.iteration_index,
                tag.workload_code,
                wl["workload_type"],
                seg.phase,
                seg.duration_sec,
            )
            _run_segment(tag, wl, seg, devices, npu, fan, current_tag, run_dir)
            prev_tag = tag
    except KeyboardInterrupt:
        logger.warning("interrupted — shutting down")
    finally:
        commands.terminate_all_subprocesses()
        npu.stop()
        server.stop()
        fan.stop()
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


def _segments() -> list[_Segment]:
    """MAX → TARGET → sweep down SWEEP_DUTIES → sweep back up, one retrace run per segment."""
    plan: tuple[tuple[str, int, tuple[int | None, ...]], ...] = (
        (_MAX_PHASE, MAX_PHASE_SEC, (None,)),
        (_TARGET_PHASE, TARGET_PHASE_SEC, (None,)),
        (_DOWN_PHASE, SWEEP_STEP_SEC, tuple(SWEEP_DUTIES)),
        (_UP_PHASE, SWEEP_STEP_SEC, tuple(reversed(SWEEP_DUTIES))),
    )
    segments: list[_Segment] = []
    for number, (kind, duration_sec, duties) in enumerate(plan, start=1):
        for i, duty in enumerate(duties):
            phase = f"{number}_{kind}" if duty is None else f"{number}_{kind}_{duty}"
            segments.append(_Segment(len(segments) + 1, kind, phase, duration_sec, duty, i == 0))
    return segments


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
        devices.append(Device(device_id=int(device_str), bus_address=bus, sid=sid[-8:]))
    devices.sort(key=lambda d: d.bus_address)
    for d in devices:
        logger.info("device rbln%d bus=%s sid=%s", d.device_id, d.bus_address, d.sid)
    return devices


def _apply_hwcfg(devices: list[Device], hwcfg: dict) -> list[Device]:
    """Engine UpdateHwCfgStep per card — write, then (hard reset → 5s → verify) up to 3 times;
    returns the cards ready for this run. A card that fails is only left out of this run."""
    logger.info("applying hw_cfg %s to %d device(s)", hwcfg, len(devices))
    pending = list(devices)
    left_out: list[Device] = []
    for attempt in range(1, _HWCFG_VERIFY_ATTEMPTS + 1):
        for d in pending:
            _write_hwcfg(d, hwcfg)
        recovered = _hard_reset(pending)
        left_out += [d for d in pending if d not in recovered]
        logger.info("delay %ds", _POST_RESET_DELAY_SEC)
        time.sleep(_POST_RESET_DELAY_SEC)
        pending = _unverified_devices(recovered, hwcfg)
        if not pending:
            break
        logger.warning(
            "hw_cfg verify FAIL on attempt %d/%d (bus %s)",
            attempt,
            _HWCFG_VERIFY_ATTEMPTS,
            [d.bus_address for d in pending],
        )
    left_out += pending
    ready = [d for d in devices if d not in left_out]
    if left_out:
        logger.error(
            "bus %s left out of this run (hw_cfg / hard reset failed); retried at the next run",
            sorted(d.bus_address for d in left_out),
        )
    logger.info("hw_cfg applied + verified on bus %s", [d.bus_address for d in ready])
    return ready


def _write_hwcfg(device: Device, hwcfg: dict) -> None:
    if commands.read_hw_cfg(device.device_id) == hwcfg:
        logger.info("%s: hw_cfg matches target, skip write", device.bus_address)
        return
    try:
        commands.write_hw_cfg(
            device.device_id, hwcfg["dcluster_clk"], hwcfg["hbm3_data_rate"], hwcfg["dnc_core_vol"]
        )
    except Exception as e:
        logger.error("%s: hw_cfg write failed: %s", device.bus_address, e)
        return
    applied = commands.read_hw_cfg(device.device_id)
    if applied != hwcfg:
        logger.error("%s: applied hw_cfg %s != %s", device.bus_address, applied, hwcfg)
        return
    logger.info("%s: hw_cfg written → %s", device.bus_address, hwcfg)


def _hard_reset(devices: list[Device]) -> list[Device]:
    """Engine ExecuteHardResetAction per card — reset in parallel, wait status=normal, then FW
    init; returns the cards that came back."""
    with ThreadPoolExecutor(max_workers=len(devices)) as pool:
        triggered = [d for d, ok in zip(devices, pool.map(_trigger_hard_reset, devices)) if ok]
    normal = _wait_for(
        triggered,
        lambda s: s.get("status") == "normal",
        "status=normal",
        _NORMAL_TIMEOUT_SEC,
        _NORMAL_POLL_SEC,
    )
    recovered = _wait_for(
        normal,
        lambda s: _num(s.get("temperature")) > 0 and _num(s.get("card_power")) > 0,
        "FW initialized (temp/power > 0)",
        _FW_INIT_TIMEOUT_SEC,
        _FW_INIT_POLL_SEC,
    )
    _refresh_device_ids(recovered)
    return recovered


def _trigger_hard_reset(device: Device) -> bool:
    try:
        commands.hard_reset(device.device_id)
    except Exception as e:
        logger.warning(
            "%s: hard reset trigger failed: %s; retrying in %ds",
            device.bus_address,
            e,
            _RESET_RETRY_DELAY_SEC,
        )
        time.sleep(_RESET_RETRY_DELAY_SEC)
        try:
            commands.hard_reset(device.device_id)
        except Exception as e2:
            logger.error("%s: hard reset trigger failed after retry: %s", device.bus_address, e2)
            return False
    logger.info("%s: hard reset triggered", device.bus_address)
    return True


def _wait_for(
    devices: list[Device],
    is_ready: Callable[[dict], bool],
    what: str,
    timeout_sec: int,
    poll_sec: int,
) -> list[Device]:
    """Poll rbln-smi until every card is ``what`` or the timeout; returns the cards that got there."""
    if not devices:
        return []
    deadline = time.monotonic() + timeout_sec
    while True:
        by_bus = _snapshot_by_bus()
        pending = [d for d in devices if not is_ready(by_bus.get(d.bus_address, {}))]
        if not pending:
            logger.info("all devices %s", what)
            return list(devices)
        if time.monotonic() >= deadline:
            logger.error(
                "bus %s not %s %ds after hard reset", [d.bus_address for d in pending], what, timeout_sec
            )
            return [d for d in devices if d not in pending]
        logger.info("waiting for %s (%d pending, sleep %ds)", what, len(pending), poll_sec)
        time.sleep(poll_sec)


def _refresh_device_ids(devices: list[Device]) -> None:
    by_bus = _snapshot_by_bus()
    for d in devices:
        name = by_bus.get(d.bus_address, {}).get("device", "").removeprefix("rbln")
        if name.isdigit() and int(name) != d.device_id:
            logger.warning("%s: rbln%d → rbln%s after hard reset", d.bus_address, d.device_id, name)
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
            logger.warning("%s (rbln%d): %s", d.bus_address, d.device_id, problem)
            unverified.append(d)
        else:
            logger.info("%s: hw_cfg verified", d.bus_address)
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


def _run_segment(
    tag: RowTag,
    wl: dict,
    seg: _Segment,
    devices: list[Device],
    npu: NpuMonitor,
    fan: FanController,
    current_tag: CurrentRowTag,
    run_dir: Path,
) -> None:
    npu.stop()
    fan.hold_max()
    current_tag.tag = replace(tag, workload_code=_HWCFG_LABEL)
    ready = _apply_hwcfg(devices, wl["hwcfg"])
    if not ready:
        raise HwCfgError(f"no device ready after hw_cfg {wl['hwcfg']}")
    current_tag.tag = tag
    npu.start(ready)
    _start_fan_phase(fan, seg)

    with ThreadPoolExecutor(max_workers=len(ready)) as executor:
        futures = [executor.submit(_retrace_device, dev, tag, wl, seg, run_dir) for dev in ready]
        for future in as_completed(futures):
            future.result()
    if seg.kind == _MAX_PHASE:
        fan.capture_baseline()
    fan.hold_max()


def _start_fan_phase(fan: FanController, seg: _Segment) -> None:
    if seg.kind == _TARGET_PHASE:
        fan.start_target()
    elif seg.duty is not None:
        fan.hold_duty(seg.duty)
    else:
        fan.hold_max()


def _retrace_device(device: Device, tag: RowTag, wl: dict, seg: _Segment, run_dir: Path) -> None:
    paths = [str(WORKLOAD_DIR / f) for f in wl["filename"]]
    label = f"{wl['code']} {seg.phase}"
    log_dir = run_dir / f"test_unit_{wl['test_unit_id']}" / f"dev_{device.path_safe_bus}"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_logs = [
        log_dir / f"{tag.iteration_index:02d}_{wl['code']}_{seg.index:02d}_{seg.phase}_{Path(f).stem}.log"
        for f in wl["filename"]
    ]

    if wl["workload_type"] == "DTC_SINGLE":
        with ThreadPoolExecutor(max_workers=len(paths)) as pool:
            futures = [
                pool.submit(_retrace_one, device, p, log, run_dir, label, seg.duration_sec, idx)
                for idx, (p, log) in enumerate(zip(paths, stdout_logs))
            ]
            for future in as_completed(futures):
                future.result()
    else:
        _retrace_one(device, paths[0], stdout_logs[0], run_dir, label, seg.duration_sec, None)


def _retrace_one(
    device: Device,
    path: str,
    stdout_log: Path,
    run_dir: Path,
    label: str,
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
            stdout_log=stdout_log,
        )
    except subprocess.TimeoutExpired as e:
        logger.error("rbln%d %s%s: retrace timed out: %s", device.device_id, label, suffix, e)
    except CommandFailedError as e:
        logger.error(
            "rbln%d %s%s: retrace failed (rc %d)%s",
            device.device_id,
            label,
            suffix,
            e.returncode,
            f": {e.stderr}" if e.stderr else "",
        )
    except Exception as e:
        logger.error("rbln%d %s%s: retrace error: %s", device.device_id, label, suffix, e)
    logger.info(
        "%s %s%s: %s",
        device.bus_address,
        label,
        suffix,
        _retrace_report(stdout_log) or f"no rblntrace Report line in {stdout_log.name}",
    )


def _retrace_report(stdout_log: Path) -> str | None:
    try:
        lines = stdout_log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if "Report:" in line:
            return line[line.index("Report:") :].strip()
    return None


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

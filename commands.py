"""Shell helpers + command wrappers over rbln / rbln-smi / rbln-smcif /
rblntrace / ipmitool.

Assumes NOPASSWD sudo. ``quiet_shell()`` suppresses the per-call command log so
the ~1 Hz collectors don't flood the console.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import signal
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import IO, Any

logger = logging.getLogger(__name__)

RBLN_SMCIF_CMD = "rbln-smcif"
RBLN_SMI_CMD = "rbln-smi"
RBLN_TRACE_CMD = "rblntrace"
IPMITOOL_CMD = "ipmitool"

_RETRACE_TIMEOUT_MARGIN_SEC = 120

# Fan control raw bytes:
#   0x30 0x70 0x66 0x02 0x01          → enter static mode
#   0x30 0x70 0x66 0x01 <zone> <duty> → set zone duty (duty byte == percent)
_FAN_STATIC_MODE = "raw 0x30 0x70 0x66 0x02 0x01"

_quiet_shell: contextvars.ContextVar[bool] = contextvars.ContextVar("quiet_shell", default=False)
_warned_cmds: set[str] = set()
_warn_lock = threading.Lock()
# PIDs (session leaders) of commands currently running, so Ctrl-C can kill them:
# subprocesses run in their own session and don't receive the terminal SIGINT.
_active_pids: set[int] = set()
_active_lock = threading.RLock()


class CommandFailedError(Exception):
    def __init__(self, cmd: str, returncode: int, stdout: str, stderr: str) -> None:
        super().__init__(f"'{cmd}' exited {returncode}: {stderr or stdout}")
        self.cmd = cmd
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# --- shell layer -----------------------------------------------------------
@contextmanager
def quiet_shell() -> Iterator[None]:
    token = _quiet_shell.set(True)
    try:
        yield
    finally:
        _quiet_shell.reset(token)


def quiet_shell_active() -> bool:
    return _quiet_shell.get()


def run_sudo_command(cmd: str, timeout: float | None = None, stream_stdout: bool = False) -> str:
    return _run(["sudo", "bash", "-lc", cmd], cmd, timeout, stream_stdout)


def run_command(cmd: str, timeout: float | None = None, stream_stdout: bool = False) -> str:
    return _run(["bash", "-lc", cmd], cmd, timeout, stream_stdout)


def terminate_all_subprocesses() -> None:
    """SIGKILL the process group of every still-running command (Ctrl-C cleanup)."""
    with _active_lock:
        pids = list(_active_pids)
    for pid in pids:
        _kill_process_group(pid)


# --- device discovery ------------------------------------------------------
def list_device_snapshots() -> dict[str, Any]:
    try:
        return _json_or_empty(run_command(f"{RBLN_SMI_CMD} -g -j"))
    except Exception as e:
        logger.error("rbln-smi -g -j failed: %s", e)
        return {}


def get_device_snapshot(device_id: int) -> dict[str, Any]:
    try:
        return _json_or_empty(run_command(f"{RBLN_SMI_CMD} -j -d {device_id}"))
    except Exception as e:
        if not quiet_shell_active():
            logger.error("rbln-smi -j -d %d failed: %s", device_id, e)
        return {}


def dmidecode_slot_for(bus_address: str) -> str:
    cmd = f"dmidecode -t slot | grep {bus_address} -B4 | grep ID: | awk '{{print $2}}'"
    try:
        return run_sudo_command(cmd)
    except Exception as e:
        logger.warning("%s failed: %s", cmd, e)
        return ""


# --- metric reads (best-effort, warn once) ---------------------------------
def read_thermal(device_id: int, chiplet_id: int | None = None) -> str:
    cmd = f"rbln sysinfo -t thermal -d {device_id}"
    if chiplet_id is not None:
        cmd += f" -c {chiplet_id}"
    return _read_warn_once(cmd)


def read_profile(device_id: int, chiplet_id: int) -> str:
    return _read_warn_once(f"rbln profile -d {device_id} -c {chiplet_id}")


def read_util(device_id: int) -> str:
    return _read_warn_once(f"rbln sysinfo -t util -d {device_id}")


def read_ucie(device_id: int, chiplet_id: int) -> str:
    return _read_warn_once(f"rbln sysinfo -t ucie -d {device_id} -c {chiplet_id}")


def read_smc_pwr(device_id: int) -> str:
    return _read_warn_once(f"rbln sysinfo -t smc_pwr -d {device_id}")


def read_smc_power_mon(slot_id: int) -> str:
    return _read_warn_once(f"{RBLN_SMCIF_CMD} power_mon -s {slot_id} -j")


def read_smc_sensor_mon(slot_id: int) -> str:
    return _read_warn_once(f"{RBLN_SMCIF_CMD} sensor_mon -s {slot_id} -j")


def read_sensor_records() -> str:
    return run_sudo_command(f"{IPMITOOL_CMD} sdr list all")


def read_dcmi_power() -> str:
    return _read_warn_once(f"{IPMITOOL_CMD} dcmi power reading")


# --- workload --------------------------------------------------------------
def retrace_workload(device_id: int, workload_path: str, test_time_sec: int, log_dir: str) -> str:
    cmd = (
        f"{RBLN_TRACE_CMD} retrace -d rbln{device_id} "
        f"--app_execution_time={test_time_sec} --get_perf=1 "
        f"-w {log_dir} {workload_path}"
    )
    return run_command(cmd, timeout=test_time_sec + _RETRACE_TIMEOUT_MARGIN_SEC, stream_stdout=True)


# --- fan control -----------------------------------------------------------
def set_fan_duty_all(duty_percent: int, zones: list[int]) -> None:
    _ipmi(_FAN_STATIC_MODE)
    for zone in zones:
        _ipmi(f"raw 0x30 0x70 0x66 0x01 {zone:#04x} {duty_percent:#04x}")


# --- hardware config -------------------------------------------------------
def write_hw_cfg(device_id: int, dcluster_clk: int, hbm3_data_rate: int, dnc_core_vol: int) -> None:
    run_sudo_command(
        f"rbln hw_cfg -d {device_id} -w "
        f"--dcluster_clk={dcluster_clk} --hbm3_data_rate={hbm3_data_rate} --dnc_core_vol={dnc_core_vol}"
    )


def hard_reset(device_id: int) -> None:
    run_sudo_command(f"echo 1 > /sys/class/rebellions/rbln{device_id}/hard_reset")


def read_hw_cfg(device_id: int) -> dict[str, int | None]:
    try:
        out = run_sudo_command(f"rbln hw_cfg -r -d {device_id}")
    except Exception as e:
        logger.warning("read hw_cfg -d %d failed: %s", device_id, e)
        out = ""
    return {
        "dcluster_clk": _find_int_field(out, "DCluster Clock"),
        "hbm3_data_rate": _find_int_field(out, "HBM3 Data Rate"),
        "dnc_core_vol": _find_int_field(out, "DNC Core Voltage"),
    }


def read_cmu_freq(device_id: int) -> dict[str, int | None]:
    try:
        out = run_sudo_command(f"rbln sysinfo -d {device_id} -t cmu_freq")
    except Exception as e:
        logger.warning("read cmu_freq -d %d failed: %s", device_id, e)
        out = ""
    return {"DRAM": _find_int_field(out, "DRAM"), "DCL0": _find_int_field(out, "DCL0")}


def read_core_voltage_mv(device_id: int) -> int | None:
    try:
        out = run_sudo_command(f"rbln sysinfo -d {device_id} -t smc_pwr")
    except Exception as e:
        logger.warning("read core voltage -d %d failed: %s", device_id, e)
        return None
    return _find_int_field(out, "rebel_core0_voltage")


# --- module-private helpers ------------------------------------------------
def _run(argv: list[str], cmd: str, timeout: float | None, stream_stdout: bool) -> str:
    if not quiet_shell_active():
        suffix = f" (timeout={timeout}s)" if timeout is not None else ""
        logger.info("[cmd] $ %s%s", cmd, suffix)
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
    )
    with _active_lock:
        _active_pids.add(proc.pid)
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    show_stderr = logger.isEnabledFor(logging.DEBUG)
    t_out = _start_drain(proc.stdout, show=stream_stdout, capture=stdout_lines)
    t_err = _start_drain(proc.stderr, show=show_stderr, capture=stderr_lines)
    try:
        proc.wait(timeout=timeout)
    except BaseException:
        _kill_process_group(proc.pid)
        raise
    finally:
        with _active_lock:
            _active_pids.discard(proc.pid)
        t_out.join(timeout=2)
        t_err.join(timeout=2)

    stdout = "\n".join(stdout_lines).strip()
    stderr = "\n".join(stderr_lines).strip()
    if proc.returncode != 0:
        raise CommandFailedError(cmd, proc.returncode, stdout, stderr)
    return stdout


def _kill_process_group(pid: int) -> None:
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _start_drain(pipe: IO[bytes] | None, *, show: bool, capture: list[str]) -> threading.Thread:
    assert pipe is not None

    def _drain() -> None:
        for raw in pipe:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if show:
                print(f"      {line}")
            capture.append(line)

    thread = threading.Thread(target=_drain, daemon=True)
    thread.start()
    return thread


def _ipmi(args: str) -> str:
    return run_sudo_command(f"{IPMITOOL_CMD} {args}")


def _read_warn_once(cmd: str) -> str:
    """Run a metric command; return "" on failure and warn about a given failing
    command only once (again only after it recovers)."""
    try:
        out = run_sudo_command(cmd)
    except Exception as e:
        with _warn_lock:
            if cmd not in _warned_cmds:
                logger.warning("%s failed: %s", cmd, e)
                _warned_cmds.add(cmd)
        return ""
    with _warn_lock:
        _warned_cmds.discard(cmd)
    return out


def _json_or_empty(output: str) -> dict[str, Any]:
    if not output.strip():
        return {}
    try:
        return json.loads(output)
    except json.JSONDecodeError as e:
        logger.error("JSON decode failed: %s", e)
        return {}


def _parse_key_value(output: str) -> dict[str, str]:
    """Parse ``key: value`` lines into a dict (first ``:`` is the separator)."""
    result: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, val = line.partition(":")
        if sep and key.strip():
            result[key.strip()] = val.strip()
    return result


def _find_int_field(output: str, label: str) -> int | None:
    """First integer in the value of any key containing ``label`` (substring match)."""
    for key, val in _parse_key_value(output).items():
        if label in key:
            m = re.search(r"(-?\d+)", val)
            if m:
                return int(m.group(1))
    return None

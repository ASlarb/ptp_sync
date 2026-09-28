"""linuxptp backend using NIC hardware timestamps and PHC discipline."""

from __future__ import annotations

import base64
import os
import re
import stat
import sys
import time
from pathlib import Path

from .base import HardwareBackend
from .models import (
    BackendStatus,
    ChangePlan,
    Check,
    Detection,
    HardwareError,
    HardwareRequest,
    PlannedChange,
)
from .runner import Runner
from .state import StateStore, TransactionState

CONFIG_PATH = Path("/etc/ptp-sync/ptp4l.conf")
PTP4L_UNIT = Path("/etc/systemd/system/ptp-sync-ptp4l.service")
PHC2SYS_UNIT = Path("/etc/systemd/system/ptp-sync-phc2sys.service")
RUNTIME_DIR = "/run/ptp-sync"
UDS_PATH = f"{RUNTIME_DIR}/ptp4l.sock"
DEFAULT_STATE_PATH = "/var/lib/ptp-sync/linuxptp-state.json"
CONFLICT_SERVICES = (
    "chronyd.service",
    "chrony.service",
    "ntpd.service",
    "ntp.service",
    "systemd-timesyncd.service",
)


def parse_ethtool_timestamping(output: str) -> dict[str, object]:
    """Parse the stable, human-readable fields from ``ethtool -T``."""
    lower = output.lower()
    phc_match = re.search(r"ptp hardware clock:\s*(-?\d+)", lower)
    return {
        "hardware_transmit": "hardware-transmit" in lower,
        "hardware_receive": "hardware-receive" in lower,
        "hardware_raw_clock": "hardware-raw-clock" in lower,
        "phc_index": int(phc_match.group(1)) if phc_match else -1,
        "tx_on": "hwtstamp_tx_on" in lower,
        "rx_ptpv2": "hwtstamp_filter_ptp_v2" in lower
        or "hwtstamp_filter_all" in lower,
    }


def parse_hwstamp_policy(output: str) -> tuple[int, int] | None:
    tx = re.search(r"tx_type\s+(\d+)", output)
    rx = re.search(r"rx_filter\s+(\d+)", output)
    if tx and rx:
        return int(tx.group(1)), int(rx.group(1))
    return None


def parse_pmc_offset(output: str) -> int | None:
    match = re.search(r"offsetFromMaster\s+(-?\d+)", output)
    return int(match.group(1)) if match else None


def parse_phc2sys_servo(output: str) -> tuple[int, str] | None:
    matches = re.findall(r"\boffset\s+(-?\d+)\s+(s\d+)\b", output)
    if not matches:
        return None
    offset, state = matches[-1]
    return int(offset), state


class LinuxPtpBackend(HardwareBackend):
    name = "linuxptp"

    def __init__(self, runner: Runner | None = None) -> None:
        self.runner = runner or Runner()

    def detect(self, request: HardwareRequest) -> Detection:
        checks: list[Check] = []
        metadata: dict[str, object] = {}
        warnings: list[str] = []
        checks.append(Check("platform", sys.platform.startswith("linux"), sys.platform))
        interface = self._resolve_interface(request.interface)
        checks.append(
            Check(
                "interface",
                interface is not None,
                interface or "specify --interface; no unique physical interface was found",
            )
        )
        metadata["interface"] = interface
        is_root = hasattr(os, "geteuid") and os.geteuid() == 0
        checks.append(Check("administrator", is_root, "root required for apply"))
        for executable in (
            "ptp4l",
            "phc2sys",
            "pmc",
            "ethtool",
            "systemctl",
            "journalctl",
        ):
            path = self.runner.which(executable)
            checks.append(Check(f"tool:{executable}", path is not None, path or "not found"))
            if path:
                metadata[f"{executable}_path"] = path
                if executable in ("ptp4l", "phc2sys", "pmc"):
                    version = self.runner.run([path, "-v"])
                    metadata[f"{executable}_version"] = (
                        version.stdout or version.stderr
                    )

        if interface and self.runner.which("ethtool"):
            result = self.runner.run(["ethtool", "-T", interface])
            capabilities = parse_ethtool_timestamping(result.stdout)
            metadata["timestamping"] = capabilities
            checks.extend(
                [
                    Check(
                        "hardware-transmit",
                        bool(capabilities["hardware_transmit"]),
                        "NIC/driver TX hardware timestamping",
                    ),
                    Check(
                        "hardware-receive",
                        bool(capabilities["hardware_receive"]),
                        "NIC/driver RX hardware timestamping",
                    ),
                    Check(
                        "hardware-raw-clock",
                        bool(capabilities["hardware_raw_clock"]),
                        "NIC PHC access",
                    ),
                    Check(
                        "phc",
                        int(capabilities["phc_index"]) >= 0,
                        f"/dev/ptp{capabilities['phc_index']}",
                    ),
                    Check(
                        "ptpv2-filter",
                        bool(capabilities["rx_ptpv2"]),
                        "PTPv2/all-packet RX filter",
                    ),
                ]
            )

        active_conflicts = [
            service
            for service in CONFLICT_SERVICES
            if self._systemctl_state("is-active", service) == "active"
        ]
        metadata["active_conflicts"] = active_conflicts
        if active_conflicts and request.role == "slave":
            warnings.append(
                "apply will stop conflicting system-clock services: "
                + ", ".join(active_conflicts)
            )
        if self.runner.which("ss"):
            sockets = self.runner.run(["ss", "-H", "-lunp"])
            occupied = [
                line
                for line in sockets.stdout.splitlines()
                if re.search(r":(?:319|320)\s", line)
            ]
            metadata["ptp_port_owners"] = occupied
            checks.append(
                Check(
                    "standard-ports",
                    not occupied,
                    "UDP 319/320 available"
                    if not occupied
                    else "occupied: " + " | ".join(occupied),
                )
            )
        if request.role == "master":
            checks.append(
                Check(
                    "utc-offset",
                    request.utc_offset is not None,
                    "Linux master requires --utc-offset from current leap-second data",
                )
            )
            synced = self._system_clock_synchronized()
            checks.append(
                Check(
                    "traceable-system-clock",
                    synced,
                    "system clock must already have a traceable UTC source",
                )
            )
        elif request.role != "slave":
            checks.append(Check("role", False, f"unsupported Linux role: {request.role}"))
        if not (0 <= request.domain <= 127):
            checks.append(Check("domain", False, "domain must be in 0..127"))

        supported = all(check.ok for check in checks if check.required)
        return Detection(self.name, sys.platform, supported, checks, metadata, warnings)

    def plan(self, request: HardwareRequest) -> ChangePlan:
        detection = self.detect(request)
        interface = str(detection.metadata.get("interface") or "")
        generated: dict[str, str] = {}
        changes: list[PlannedChange] = []
        if interface:
            generated["ptp4l.conf"] = self._render_config(request, interface)
            generated[PTP4L_UNIT.name] = self._render_ptp4l_unit(request, interface)
            generated[PHC2SYS_UNIT.name] = self._render_phc2sys_unit(
                request, interface
            )
        changes.extend(
            [
                PlannedChange("write", str(CONFIG_PATH), "app-owned linuxptp configuration"),
                PlannedChange("write", str(PTP4L_UNIT), "app-owned ptp4l systemd unit"),
                PlannedChange(
                    "write", str(PHC2SYS_UNIT), "app-owned phc2sys systemd unit"
                ),
            ]
        )
        if request.role == "slave":
            for service in detection.metadata.get("active_conflicts", []):
                changes.append(
                    PlannedChange(
                        "stop",
                        str(service),
                        "avoid multiple processes disciplining CLOCK_REALTIME",
                        disruptive=True,
                    )
                )
        changes.extend(
            [
                PlannedChange(
                    "enable",
                    PTP4L_UNIT.name,
                    f"run hardware PTP as {request.role}",
                    disruptive=True,
                ),
                PlannedChange(
                    "enable",
                    PHC2SYS_UNIT.name,
                    "discipline PHC/system clock",
                    disruptive=True,
                ),
            ]
        )
        return ChangePlan(
            self.name,
            request.role,
            detection.supported,
            detection.checks,
            changes,
            detection.warnings,
            generated,
        )

    def apply(self, request: HardwareRequest) -> BackendStatus:
        plan = self.plan(request)
        if not plan.applicable:
            failed = ", ".join(check.name for check in plan.checks if not check.ok)
            raise HardwareError(f"linuxptp preflight failed: {failed}")
        if not request.assume_yes and any(change.disruptive for change in plan.changes):
            raise HardwareError("disruptive changes require --yes")

        store = StateStore(request.state_file or DEFAULT_STATE_PATH)
        if store.exists():
            raise HardwareError(
                f"transaction state already exists at {store.path}; restore it first"
            )
        interface = next(
            check.detail for check in plan.checks if check.name == "interface"
        )
        state = self._snapshot(interface)
        store.save(state)
        try:
            if request.role == "slave":
                for service, saved in state.values["services"].items():
                    if saved["active"]:
                        self.runner.run(["systemctl", "stop", service], check=True)
            self._write_owned(CONFIG_PATH, plan.generated["ptp4l.conf"])
            self._write_owned(PTP4L_UNIT, plan.generated[PTP4L_UNIT.name])
            self._write_owned(PHC2SYS_UNIT, plan.generated[PHC2SYS_UNIT.name])
            self.runner.run(["systemctl", "daemon-reload"], check=True)
            self.runner.run(
                ["systemctl", "enable", "--now", PTP4L_UNIT.name], check=True
            )
            self.runner.run(
                ["systemctl", "enable", "--now", PHC2SYS_UNIT.name], check=True
            )
            status = self._wait_for_lock(request)
            if not status.healthy:
                raise HardwareError(
                    "linuxptp services started but did not reach the requested PTP state"
                )
            state.completed = True
            store.save(state)
        except BaseException as exc:
            try:
                self._restore_state(state)
                store.remove()
            except BaseException as rollback_exc:
                raise HardwareError(
                    f"linuxptp apply failed ({exc}); rollback also failed: {rollback_exc}"
                ) from exc
            raise
        return self.status(request)

    def status(self, request: HardwareRequest) -> BackendStatus:
        ptp4l = self._systemctl_state("is-active", PTP4L_UNIT.name)
        phc2sys = self._systemctl_state("is-active", PHC2SYS_UNIT.name)
        details = [f"{PTP4L_UNIT.name}: {ptp4l}", f"{PHC2SYS_UNIT.name}: {phc2sys}"]
        metrics: dict[str, object] = {}
        pmc = self.runner.which("pmc")
        expected_state = "MASTER" if request.role == "master" else "SLAVE"
        port_ok = False
        if pmc and ptp4l == "active":
            port = self.runner.run(
                [
                    pmc,
                    "-u",
                    "-b",
                    "0",
                    "-d",
                    str(request.domain),
                    "-s",
                    UDS_PATH,
                    "GET PORT_DATA_SET",
                ]
            )
            current = self.runner.run(
                [
                    pmc,
                    "-u",
                    "-b",
                    "0",
                    "-d",
                    str(request.domain),
                    "-s",
                    UDS_PATH,
                    "GET CURRENT_DATA_SET",
                ]
            )
            port_ok = bool(
                re.search(rf"portState\s+{re.escape(expected_state)}\b", port.stdout)
            )
            offset = parse_pmc_offset(current.stdout)
            offset_ok = request.role == "master" or (
                offset is not None and abs(offset) <= 1_000_000
            )
            metrics["port_state"] = expected_state if port_ok else "unknown"
            metrics["offset_from_master_ns"] = offset
            if not port_ok:
                details.append(f"PTP port has not reached {expected_state}")
            if not offset_ok:
                details.append("offsetFromMaster has not converged below 1 ms")
        else:
            offset_ok = False
        servo_ok = False
        if phc2sys == "active" and self.runner.which("journalctl"):
            journal = self.runner.run(
                [
                    "journalctl",
                    "-u",
                    PHC2SYS_UNIT.name,
                    "-n",
                    "30",
                    "--no-pager",
                    "-o",
                    "cat",
                ]
            )
            servo = parse_phc2sys_servo(journal.stdout)
            if servo is not None:
                servo_offset, servo_state = servo
                metrics["phc2sys_offset_ns"] = servo_offset
                metrics["phc2sys_servo_state"] = servo_state
                servo_ok = servo_state == "s2" and abs(servo_offset) <= 1_000_000
            if not servo_ok:
                details.append("phc2sys servo has not converged below 1 ms in state s2")
        healthy = (
            ptp4l == "active"
            and phc2sys == "active"
            and port_ok
            and offset_ok
            and servo_ok
        )
        return BackendStatus(
            self.name,
            healthy,
            "locked" if healthy else "not-synchronized",
            details,
            metrics,
        )

    def _wait_for_lock(
        self, request: HardwareRequest, timeout: float = 30.0
    ) -> BackendStatus:
        deadline = time.monotonic() + timeout
        last = self.status(request)
        while not last.healthy and time.monotonic() < deadline:
            time.sleep(1.0)
            last = self.status(request)
        return last

    def restore(self, request: HardwareRequest) -> BackendStatus:
        store = StateStore(request.state_file or DEFAULT_STATE_PATH)
        if not store.exists():
            return BackendStatus(
                self.name, True, "not-configured", ["no transaction state exists"]
            )
        state = store.load()
        if state.backend != self.name:
            raise HardwareError(f"state belongs to backend {state.backend}")
        self._restore_state(state)
        store.remove()
        return BackendStatus(self.name, True, "restored", ["prior state restored"])

    def _resolve_interface(self, requested: str | None) -> str | None:
        if requested:
            if not re.fullmatch(r"[A-Za-z0-9_.:-]+", requested):
                return None
            return requested if Path("/sys/class/net", requested).exists() else None
        root = Path("/sys/class/net")
        if not root.is_dir():
            return None
        candidates = [
            path.name
            for path in root.iterdir()
            if path.name != "lo" and (path / "device").exists()
        ]
        return candidates[0] if len(candidates) == 1 else None

    def _systemctl_state(self, verb: str, service: str) -> str:
        if not self.runner.which("systemctl"):
            return "unavailable"
        result = self.runner.run(["systemctl", verb, service])
        return result.stdout or ("yes" if result.ok else "no")

    def _system_clock_synchronized(self) -> bool:
        if self.runner.which("timedatectl"):
            result = self.runner.run(
                ["timedatectl", "show", "-p", "NTPSynchronized", "--value"]
            )
            return result.ok and result.stdout.lower() == "yes"
        return False

    def _render_config(self, request: HardwareRequest, interface: str) -> str:
        role_option = ""
        if request.role == "master":
            role_option = f"{self._server_only_option()}        1\n"
        return (
            "[global]\n"
            f"domainNumber             {request.domain}\n"
            "network_transport        UDPv4\n"
            "delay_mechanism          E2E\n"
            "time_stamping            hardware\n"
            "twoStepFlag              1\n"
            f"{role_option}"
            f"uds_address              {UDS_PATH}\n"
            f"uds_ro_address           {RUNTIME_DIR}/ptp4lro.sock\n"
            "summary_interval         0\n"
            "\n"
            f"[{interface}]\n"
        )

    def _render_ptp4l_unit(self, request: HardwareRequest, interface: str) -> str:
        executable = self.runner.which("ptp4l") or "/usr/sbin/ptp4l"
        args = f"{executable} -i {interface} -f {CONFIG_PATH} -m -q"
        if request.role == "slave":
            args += " -s"
        lines = [
            "[Unit]",
            "Description=PTP Sync managed linuxptp daemon",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"RuntimeDirectory={Path(RUNTIME_DIR).name}",
            f"ExecStart={args}",
            "Restart=on-failure",
            "RestartSec=2",
        ]
        if request.role == "master":
            pmc = self.runner.which("pmc") or "/usr/sbin/pmc"
            sleep = self.runner.which("sleep") or "/usr/bin/sleep"
            command = (
                "SET GRANDMASTER_SETTINGS_NP clockClass 248 clockAccuracy 0xfe "
                "offsetScaledLogVariance 0xffff "
                f"currentUtcOffset {request.utc_offset} leap61 0 leap59 0 "
                "currentUtcOffsetValid 1 ptpTimescale 1 timeTraceable 1 "
                "frequencyTraceable 1 timeSource 0x50"
            )
            lines.append(f"ExecStartPost={sleep} 1")
            lines.append(
                f'ExecStartPost={pmc} -u -b 0 -d {request.domain} '
                f'-s {UDS_PATH} "{command}"'
            )
        lines.extend(["", "[Install]", "WantedBy=multi-user.target", ""])
        return "\n".join(lines)

    def _server_only_option(self) -> str:
        executable = self.runner.which("ptp4l")
        if executable:
            version = self.runner.run([executable, "-v"])
            match = re.search(r"(\d+)(?:\.\d+)?", version.stdout + version.stderr)
            if match and int(match.group(1)) >= 4:
                return "serverOnly"
        return "masterOnly"

    def _render_phc2sys_unit(self, request: HardwareRequest, interface: str) -> str:
        executable = self.runner.which("phc2sys") or "/usr/sbin/phc2sys"
        source, sink = (
            ("CLOCK_REALTIME", interface)
            if request.role == "master"
            else (interface, "CLOCK_REALTIME")
        )
        return "\n".join(
            [
                "[Unit]",
                "Description=PTP Sync PHC/system clock servo",
                f"After={PTP4L_UNIT.name}",
                f"Requires={PTP4L_UNIT.name}",
                "",
                "[Service]",
                "Type=simple",
                (
                    f"ExecStart={executable} -s {source} -c {sink} -w "
                    f"-n {request.domain} -z {UDS_PATH} -m"
                ),
                "Restart=on-failure",
                "RestartSec=2",
                "",
                "[Install]",
                "WantedBy=multi-user.target",
                "",
            ]
        )

    def _snapshot(self, interface: str) -> TransactionState:
        files: dict[str, object] = {}
        for path in (CONFIG_PATH, PTP4L_UNIT, PHC2SYS_UNIT):
            if path.exists():
                raw = path.read_bytes()
                files[str(path)] = {
                    "exists": True,
                    "content_b64": base64.b64encode(raw).decode("ascii"),
                    "mode": stat.S_IMODE(path.stat().st_mode),
                }
            else:
                files[str(path)] = {"exists": False}
        services = {
            service: {
                "active": self._systemctl_state("is-active", service) == "active",
                "enabled": self._systemctl_state("is-enabled", service)
                in ("enabled", "static"),
            }
            for service in CONFLICT_SERVICES
        }
        managed_services = {
            service: {
                "active": self._systemctl_state("is-active", service) == "active",
                "enabled": self._systemctl_state("is-enabled", service)
                in ("enabled", "static"),
            }
            for service in (PTP4L_UNIT.name, PHC2SYS_UNIT.name)
        }
        hwstamp = None
        if self.runner.which("hwstamp_ctl"):
            result = self.runner.run(["hwstamp_ctl", "-i", interface])
            hwstamp = parse_hwstamp_policy(result.stdout)
        return TransactionState(
            self.name,
            values={
                "interface": interface,
                "files": files,
                "services": services,
                "managed_services": managed_services,
                "hwstamp": list(hwstamp) if hwstamp else None,
            },
            owned_paths=[str(CONFIG_PATH), str(PTP4L_UNIT), str(PHC2SYS_UNIT)],
        )

    def _restore_state(self, state: TransactionState) -> None:
        for path, service in (
            (PHC2SYS_UNIT, PHC2SYS_UNIT.name),
            (PTP4L_UNIT, PTP4L_UNIT.name),
        ):
            if path.exists():
                self.runner.run(
                    ["systemctl", "disable", "--now", service], check=True
                )
        for raw_path, saved in state.values.get("files", {}).items():
            path = Path(raw_path)
            if saved.get("exists"):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(base64.b64decode(saved["content_b64"]))
                path.chmod(int(saved["mode"]))
            else:
                path.unlink(missing_ok=True)
        self.runner.run(["systemctl", "daemon-reload"], check=True)
        hwstamp = state.values.get("hwstamp")
        if hwstamp and self.runner.which("hwstamp_ctl"):
            self.runner.run(
                [
                    "hwstamp_ctl",
                    "-i",
                    str(state.values["interface"]),
                    "-t",
                    str(hwstamp[0]),
                    "-r",
                    str(hwstamp[1]),
                ],
                check=True,
            )
        for service, saved in state.values.get("managed_services", {}).items():
            if saved.get("enabled"):
                self.runner.run(["systemctl", "enable", service], check=True)
            if saved.get("active"):
                self.runner.run(["systemctl", "start", service], check=True)
        for service, saved in state.values.get("services", {}).items():
            if saved.get("active"):
                self.runner.run(["systemctl", "start", service], check=True)

    @staticmethod
    def _write_owned(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(0o644)


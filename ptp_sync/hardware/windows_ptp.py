"""Windows W32Time PTP client backend with NDIS capability validation."""

from __future__ import annotations

import ctypes
import ipaddress
import json
import os
import re
import socket
import sys
import time
from pathlib import Path
from typing import Any

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

PTP_KEY = r"HKLM\SYSTEM\CurrentControlSet\Services\W32Time\TimeProviders\PtpClient"
NTP_KEY = r"HKLM\SYSTEM\CurrentControlSet\Services\W32Time\TimeProviders\NtpClient"
VMIC_KEY = (
    r"HKLM\SYSTEM\CurrentControlSet\Services\W32Time"
    r"\TimeProviders\VMICTimeProvider"
)
FIREWALL_RULES = ("PTP-Sync-UDP-319", "PTP-Sync-UDP-320")


class _HardwareCaps(ctypes.Structure):
    _fields_ = [
        ("ipv4_event_rx", ctypes.c_ubyte),
        ("ipv4_all_rx", ctypes.c_ubyte),
        ("ipv4_event_tx", ctypes.c_ubyte),
        ("ipv4_all_tx", ctypes.c_ubyte),
        ("ipv6_event_rx", ctypes.c_ubyte),
        ("ipv6_all_rx", ctypes.c_ubyte),
        ("ipv6_event_tx", ctypes.c_ubyte),
        ("ipv6_all_tx", ctypes.c_ubyte),
        ("all_rx", ctypes.c_ubyte),
        ("all_tx", ctypes.c_ubyte),
        ("tagged_tx", ctypes.c_ubyte),
    ]


class _SoftwareCaps(ctypes.Structure):
    _fields_ = [
        ("all_rx", ctypes.c_ubyte),
        ("all_tx", ctypes.c_ubyte),
        ("tagged_tx", ctypes.c_ubyte),
    ]


class _TimestampCaps(ctypes.Structure):
    _fields_ = [
        ("hardware_clock_hz", ctypes.c_uint64),
        ("cross_timestamp", ctypes.c_ubyte),
        ("hardware", _HardwareCaps),
        ("software", _SoftwareCaps),
    ]


def _hardware_ipv4(caps: _TimestampCaps) -> bool:
    rx = caps.hardware.ipv4_event_rx or caps.hardware.ipv4_all_rx or caps.hardware.all_rx
    tx = (
        caps.hardware.ipv4_event_tx
        or caps.hardware.ipv4_all_tx
        or caps.hardware.tagged_tx
        or caps.hardware.all_tx
    )
    return bool(rx and tx)


def query_windows_timestamp_capabilities(interface: str) -> dict[str, Any]:
    """Query authoritative supported and active timestamp capabilities."""
    if sys.platform != "win32":
        raise HardwareError("Windows timestamp APIs are only available on Windows")
    try:
        index = socket.if_nametoindex(interface)
    except OSError as exc:
        raise HardwareError(f"cannot resolve Windows interface {interface}: {exc}") from exc
    iphlp = ctypes.WinDLL("iphlpapi", use_last_error=True)
    luid = ctypes.c_uint64()
    convert = iphlp.ConvertInterfaceIndexToLuid
    convert.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint64)]
    convert.restype = ctypes.c_ulong
    rc = convert(index, ctypes.byref(luid))
    if rc:
        raise HardwareError(f"ConvertInterfaceIndexToLuid failed: {rc}")

    def query(name: str) -> tuple[int, _TimestampCaps]:
        caps = _TimestampCaps()
        function = getattr(iphlp, name)
        function.argtypes = [
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(_TimestampCaps),
        ]
        function.restype = ctypes.c_ulong
        return function(ctypes.byref(luid), ctypes.byref(caps)), caps

    supported_rc, supported = query("GetInterfaceSupportedTimestampCapabilities")
    active_rc, active = query("GetInterfaceActiveTimestampCapabilities")
    return {
        "supported_rc": supported_rc,
        "active_rc": active_rc,
        "supported_ipv4_hardware": supported_rc == 0 and _hardware_ipv4(supported),
        "active_ipv4_hardware": active_rc == 0 and _hardware_ipv4(active),
        "supported_cross_timestamp": bool(supported.cross_timestamp),
        "active_cross_timestamp": bool(active.cross_timestamp),
        "hardware_clock_hz": int(supported.hardware_clock_hz),
    }


def parse_reg_query(output: str) -> dict[str, dict[str, str]]:
    values: dict[str, dict[str, str]] = {}
    for line in output.splitlines():
        match = re.match(r"\s+(\S+)\s+(REG_\S+)\s*(.*)$", line)
        if match:
            values[match.group(1)] = {
                "type": match.group(2),
                "data": match.group(3),
            }
    return values


class WindowsPtpBackend(HardwareBackend):
    name = "windows-ptp"

    def __init__(self, runner: Runner | None = None) -> None:
        self.runner = runner or Runner()

    def detect(self, request: HardwareRequest) -> Detection:
        checks: list[Check] = []
        metadata: dict[str, Any] = {}
        warnings: list[str] = []
        is_windows = sys.platform == "win32"
        checks.append(Check("platform", is_windows, sys.platform))
        if request.role != "slave":
            checks.append(
                Check(
                    "role",
                    False,
                    "native W32Time PTP supports client/slave only; use an external GM",
                )
            )
        if not is_windows:
            return Detection(self.name, sys.platform, False, checks, metadata, warnings)

        build = int(sys.getwindowsversion().build)
        metadata["windows_build"] = build
        checks.append(Check("windows-build", build >= 20348, f"build {build}; need 20348+"))
        admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        checks.append(Check("administrator", admin, "elevated Administrator required"))
        provider = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "ptpprov.dll"
        checks.append(Check("ptpprov.dll", provider.is_file(), str(provider)))
        powershell = self._powershell()
        checks.append(Check("powershell", powershell is not None, powershell or "not found"))
        metadata["extended_ptp_options"] = self._supports_extended_ptp()

        interface = request.interface or self._single_physical_interface()
        metadata["interface"] = interface
        checks.append(
            Check(
                "interface",
                bool(interface),
                interface or "specify --interface; no unique connected physical NIC",
            )
        )
        valid_masters = self._valid_masters(request.masters)
        metadata["masters"] = valid_masters
        checks.append(
            Check(
                "grandmaster",
                bool(valid_masters) and len(valid_masters) == len(request.masters),
                "at least one valid IPv4 --master is required",
            )
        )
        if not 0 <= request.domain <= 127:
            checks.append(Check("domain", False, "domain must be in 0..127"))
        elif request.domain != 0:
            domain_supported = bool(metadata["extended_ptp_options"])
            checks.append(
                Check(
                    "domain",
                    domain_supported,
                    "nonzero domain requires a build/provider exposing DomainNumber",
                )
            )

        property_present = False
        if powershell and interface:
            script = (
                "$p=Get-NetAdapterAdvancedProperty -Name $env:PTP_INTERFACE "
                "-RegistryKeyword '*PtpHardwareTimestamp' -ErrorAction SilentlyContinue;"
                "if($null -eq $p){exit 3};"
                "$p|Select-Object RegistryKeyword,RegistryValue,DisplayValue|ConvertTo-Json -Compress"
            )
            result = self.runner.run(
                [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
                env={"PTP_INTERFACE": interface},
            )
            property_present = result.ok
            if result.ok and result.stdout:
                try:
                    metadata["advanced_property"] = json.loads(result.stdout)
                except json.JSONDecodeError:
                    warnings.append("could not parse *PtpHardwareTimestamp property")
        checks.append(
            Check(
                "driver-property",
                property_present,
                "driver must expose standardized *PtpHardwareTimestamp",
            )
        )

        if interface:
            try:
                capabilities = query_windows_timestamp_capabilities(interface)
                metadata["timestamping"] = capabilities
                checks.extend(
                    [
                        Check(
                            "hardware-ipv4-timestamping",
                            bool(capabilities["supported_ipv4_hardware"]),
                            "NDIS reports PTPv2 UDP/IPv4 hardware RX and TX",
                        ),
                        Check(
                            "cross-timestamp",
                            bool(capabilities["supported_cross_timestamp"]),
                            "NDIS hardware/system cross timestamp",
                        ),
                    ]
                )
                if not capabilities["active_ipv4_hardware"]:
                    warnings.append(
                        "hardware timestamping is supported but inactive; apply will enable it"
                    )
            except HardwareError as exc:
                checks.append(Check("timestamp-api", False, str(exc)))

        supported = all(check.ok for check in checks if check.required)
        return Detection(self.name, sys.platform, supported, checks, metadata, warnings)

    def plan(self, request: HardwareRequest) -> ChangePlan:
        detection = self.detect(request)
        interface = detection.metadata.get("interface") or request.interface or ""
        masters = detection.metadata.get("masters") or []
        changes = [
            PlannedChange(
                "enable",
                f"{interface}:*PtpHardwareTimestamp",
                "enable NDIS hardware PTP timestamps",
                disruptive=True,
            ),
            PlannedChange(
                "restart",
                str(interface),
                "restart network adapter so timestamping takes effect",
                disruptive=True,
            ),
            PlannedChange(
                "configure",
                PTP_KEY,
                f"enable W32Time PtpClient for {', '.join(masters)}",
            ),
            PlannedChange(
                "configure",
                "Windows Firewall",
                "allow inbound UDP 319 and 320",
            ),
            PlannedChange(
                "disable",
                "NtpClient/VMICTimeProvider",
                "avoid competing W32Time input providers",
                disruptive=True,
            ),
            PlannedChange(
                "restart", "W32Time", "load PTP provider configuration", disruptive=True
            ),
        ]
        return ChangePlan(
            self.name,
            request.role,
            detection.supported,
            detection.checks,
            changes,
            detection.warnings,
        )

    def apply(self, request: HardwareRequest) -> BackendStatus:
        plan = self.plan(request)
        if not plan.applicable:
            failed = ", ".join(check.name for check in plan.checks if not check.ok)
            raise HardwareError(f"Windows PTP preflight failed: {failed}")
        if not request.assume_yes:
            raise HardwareError("adapter/service restart requires --yes")
        interface = next(
            check.detail for check in plan.checks if check.name == "interface"
        )
        store = StateStore(request.state_file or self._default_state_path())
        if store.exists():
            raise HardwareError(
                f"transaction state already exists at {store.path}; restore it first"
            )
        state = self._snapshot(interface)
        store.save(state)
        powershell = self._require_powershell()
        try:
            self.runner.run(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    (
                        "Set-NetAdapterAdvancedProperty -Name $env:PTP_INTERFACE "
                        "-RegistryKeyword '*PtpHardwareTimestamp' -RegistryValue 1 "
                        "-NoRestart -ErrorAction Stop;"
                        "Restart-NetAdapter -Name $env:PTP_INTERFACE -Confirm:$false "
                        "-ErrorAction Stop"
                    ),
                ],
                check=True,
                timeout=60,
                env={"PTP_INTERFACE": interface},
            )
            self._configure_provider(request)
            self._configure_firewall()
            self._restart_w32time()
            status = self._wait_for_lock(request)
            if not status.healthy:
                raise HardwareError(
                    "W32Time started but did not lock to the requested PTP master"
                )
            state.completed = True
            store.save(state)
        except BaseException as exc:
            try:
                self._restore_state(state)
                store.remove()
            except BaseException as rollback_exc:
                raise HardwareError(
                    f"Windows PTP apply failed ({exc}); rollback also failed: {rollback_exc}"
                ) from exc
            raise
        return self.status(request)

    def status(self, request: HardwareRequest) -> BackendStatus:
        if sys.platform != "win32":
            return BackendStatus(
                self.name, False, "unsupported", ["Windows backend on non-Windows host"]
            )
        service_running = self._service_running()
        source = self.runner.run(["w32tm.exe", "/query", "/source"])
        status = self.runner.run(["w32tm.exe", "/query", "/status", "/verbose"])
        powershell = self._powershell()
        event_ok = False
        if powershell:
            event = self.runner.run(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    (
                        "$e=Get-WinEvent -FilterHashtable @{"
                        "LogName='Microsoft-Windows-Time-Service-PTP-Provider/PTP-Operational';"
                        "Id=513} "
                        "-ErrorAction SilentlyContinue|Select-Object -First 1;"
                        "if($null -eq $e){exit 3}"
                    ),
                ]
            )
            event_ok = event.ok
        service_ok = service_running
        interface = request.interface or self._single_physical_interface()
        hardware_ok = False
        if interface:
            try:
                capabilities = query_windows_timestamp_capabilities(interface)
                hardware_ok = bool(
                    capabilities["active_ipv4_hardware"]
                    and capabilities["active_cross_timestamp"]
                )
            except HardwareError:
                hardware_ok = False
        if request.masters:
            source_ok = source.ok and any(
                master in source.stdout for master in request.masters
            )
        else:
            source_ok = source.ok and bool(source.stdout) and not any(
                marker in source.stdout.lower()
                for marker in ("local cmos", "free-running", "unspecified")
            )
        status_lower = status.stdout.lower()
        explicitly_unsynchronized = any(
            marker in status_lower
            for marker in ("not synchronized", "unsynchronized", "未同步")
        )
        provider_ok = "0X4D505450" in status.stdout.upper()
        configured_domain = self._registry_dword(PTP_KEY, "DomainNumber")
        domain_ok = (
            configured_domain == request.domain
            if configured_domain is not None
            else request.domain == 0
        )
        sync_ok = (
            status.ok
            and bool(status.stdout)
            and not explicitly_unsynchronized
            and provider_ok
        )
        healthy = (
            service_ok
            and source_ok
            and sync_ok
            and event_ok
            and hardware_ok
            and domain_ok
        )
        details = [
            f"W32Time running: {service_ok}",
            f"source: {source.stdout or source.stderr or 'unknown'}",
            f"PTP master selection event present: {event_ok}",
            f"PTP provider reference ID: {provider_ok}",
            f"active NDIS hardware timestamps: {hardware_ok}",
            f"domain {request.domain} configured: {domain_ok}",
        ]
        return BackendStatus(
            self.name,
            healthy,
            "locked" if healthy else "not-synchronized",
            details,
            {"source": source.stdout, "status": status.stdout},
        )

    def _wait_for_lock(
        self, request: HardwareRequest, timeout: float = 60.0
    ) -> BackendStatus:
        deadline = time.monotonic() + timeout
        last = self.status(request)
        while not last.healthy and time.monotonic() < deadline:
            time.sleep(2.0)
            last = self.status(request)
        return last

    def restore(self, request: HardwareRequest) -> BackendStatus:
        store = StateStore(request.state_file or self._default_state_path())
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

    def _powershell(self) -> str | None:
        return self.runner.which("powershell.exe") or self.runner.which("pwsh.exe")

    def _require_powershell(self) -> str:
        executable = self._powershell()
        if not executable:
            raise HardwareError("PowerShell is required")
        return executable

    def _service_running(self) -> bool:
        powershell = self._powershell()
        if not powershell:
            return False
        result = self.runner.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "if((Get-Service W32Time -ErrorAction Stop).Status -eq 'Running'){exit 0}else{exit 3}",
            ]
        )
        return result.ok

    def _restart_w32time(self) -> None:
        powershell = self._require_powershell()
        self.runner.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    "$s=Get-Service W32Time -ErrorAction Stop;"
                    "if($s.Status -ne 'Stopped'){Stop-Service W32Time -Force -ErrorAction Stop;"
                    "$s.WaitForStatus('Stopped',[TimeSpan]::FromSeconds(30))};"
                    "Start-Service W32Time -ErrorAction Stop;"
                    "$s.WaitForStatus('Running',[TimeSpan]::FromSeconds(30))"
                ),
            ],
            check=True,
            timeout=70,
        )

    def _stop_w32time(self) -> None:
        powershell = self._require_powershell()
        self.runner.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    "$s=Get-Service W32Time -ErrorAction Stop;"
                    "if($s.Status -ne 'Stopped'){Stop-Service W32Time -Force -ErrorAction Stop;"
                    "$s.WaitForStatus('Stopped',[TimeSpan]::FromSeconds(30))}"
                ),
            ],
            check=True,
            timeout=40,
        )

    def _single_physical_interface(self) -> str | None:
        powershell = self._powershell()
        if not powershell:
            return None
        result = self.runner.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    "Get-NetAdapter -Physical|Where-Object Status -eq 'Up'|"
                    "Select-Object -ExpandProperty Name|ConvertTo-Json -Compress"
                ),
            ]
        )
        if not result.ok or not result.stdout:
            return None
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        if isinstance(value, str):
            return value
        return value[0] if isinstance(value, list) and len(value) == 1 else None

    @staticmethod
    def _valid_masters(values: list[str]) -> list[str]:
        valid: list[str] = []
        for value in values:
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            if address.version == 4 and not address.is_multicast:
                valid.append(str(address))
        return valid

    def _registry_value_exists(self, key: str, value: str) -> bool:
        result = self.runner.run(["reg.exe", "query", key, "/v", value])
        return result.ok

    def _registry_dword(self, key: str, value: str) -> int | None:
        result = self.runner.run(["reg.exe", "query", key, "/v", value])
        if not result.ok:
            return None
        parsed = parse_reg_query(result.stdout).get(value)
        if not parsed or parsed["type"] != "REG_DWORD":
            return None
        try:
            return int(parsed["data"], 0)
        except ValueError:
            return None

    def _supports_extended_ptp(self) -> bool:
        if sys.platform != "win32" or not self.runner.which("w32tm.exe"):
            return False
        help_result = self.runner.run(["w32tm.exe", "/?"])
        return "ptp_monitor" in (help_result.stdout + help_result.stderr).lower()

    def _configure_provider(self, request: HardwareRequest) -> None:
        values = (
            ("PtpMasters", "REG_SZ", " ".join(self._valid_masters(request.masters))),
            ("Enabled", "REG_DWORD", "1"),
            ("InputProvider", "REG_DWORD", "1"),
            ("DllName", "REG_EXPAND_SZ", r"%SystemRoot%\System32\ptpprov.dll"),
            ("DelayPollInterval", "REG_DWORD", "0x3e80"),
            ("AnnounceInterval", "REG_DWORD", "0x0fa0"),
            ("EnableMulticastRx", "REG_DWORD", "1"),
        )
        for name, kind, data in values:
            self.runner.run(
                ["reg.exe", "add", PTP_KEY, "/v", name, "/t", kind, "/d", data, "/f"],
                check=True,
            )
        if self._supports_extended_ptp():
            for name, data in (
                ("EnableMulticastTx", "1"),
                ("DomainNumber", str(request.domain)),
            ):
                self.runner.run(
                    [
                        "reg.exe",
                        "add",
                        PTP_KEY,
                        "/v",
                        name,
                        "/t",
                        "REG_DWORD",
                        "/d",
                        data,
                        "/f",
                    ],
                    check=True,
                )
        for key in (NTP_KEY, VMIC_KEY):
            self.runner.run(
                [
                    "reg.exe",
                    "add",
                    key,
                    "/v",
                    "Enabled",
                    "/t",
                    "REG_DWORD",
                    "/d",
                    "0",
                    "/f",
                ],
                check=True,
            )

    def _configure_firewall(self) -> None:
        powershell = self._require_powershell()
        for port, name in zip((319, 320), FIREWALL_RULES):
            script = (
                "if(-not (Get-NetFirewallRule -Name $env:PTP_RULE "
                "-ErrorAction SilentlyContinue)){"
                "New-NetFirewallRule -Name $env:PTP_RULE -DisplayName $env:PTP_RULE "
                "-Direction Inbound -Protocol UDP -LocalPort $env:PTP_PORT "
                "-Action Allow -ErrorAction Stop|Out-Null}"
            )
            self.runner.run(
                [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
                check=True,
                env={"PTP_RULE": name, "PTP_PORT": str(port)},
            )

    def _snapshot(self, interface: str) -> TransactionState:
        registry = {
            key: self._query_registry_key(key) for key in (PTP_KEY, NTP_KEY, VMIC_KEY)
        }
        powershell = self._require_powershell()
        adapter = self.runner.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    "$p=Get-NetAdapterAdvancedProperty -Name $env:PTP_INTERFACE "
                    "-RegistryKeyword '*PtpHardwareTimestamp' -ErrorAction Stop;"
                    "$p.RegistryValue|ConvertTo-Json -Compress"
                ),
            ],
            check=True,
            env={"PTP_INTERFACE": interface},
        )
        firewall = {
            name: self.runner.run(
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    (
                        "if(Get-NetFirewallRule -Name $env:PTP_RULE "
                        "-ErrorAction SilentlyContinue){exit 0}else{exit 3}"
                    ),
                ],
                env={"PTP_RULE": name},
            ).ok
            for name in FIREWALL_RULES
        }
        service_running = self._service_running()
        adapter_value = json.loads(adapter.stdout)
        if isinstance(adapter_value, list):
            if len(adapter_value) != 1:
                raise HardwareError(
                    "unexpected multiple *PtpHardwareTimestamp registry values"
                )
            adapter_value = adapter_value[0]
        return TransactionState(
            self.name,
            values={
                "interface": interface,
                "registry": registry,
                "adapter_value": adapter_value,
                "firewall": firewall,
                "service_running": service_running,
            },
        )

    def _query_registry_key(self, key: str) -> dict[str, Any]:
        result = self.runner.run(["reg.exe", "query", key])
        return {"exists": result.ok, "values": parse_reg_query(result.stdout)}

    def _restore_state(self, state: TransactionState) -> None:
        powershell = self._require_powershell()
        self._stop_w32time()
        for key, saved in state.values["registry"].items():
            if key == PTP_KEY:
                if not saved["exists"]:
                    if self._query_registry_key(key)["exists"]:
                        self.runner.run(
                            ["reg.exe", "delete", key, "/f"], check=True
                        )
                    continue
                current = self._query_registry_key(key)
                for name in current["values"]:
                    if name not in saved["values"]:
                        self.runner.run(
                            ["reg.exe", "delete", key, "/v", name, "/f"],
                            check=True,
                        )
                values_to_restore = saved["values"]
            else:
                original_enabled = saved["values"].get("Enabled")
                if original_enabled is None:
                    if self._registry_value_exists(key, "Enabled"):
                        self.runner.run(
                            ["reg.exe", "delete", key, "/v", "Enabled", "/f"],
                            check=True,
                        )
                    continue
                values_to_restore = {"Enabled": original_enabled}
            for name, value in values_to_restore.items():
                self.runner.run(
                    [
                        "reg.exe",
                        "add",
                        key,
                        "/v",
                        name,
                        "/t",
                        value["type"],
                        "/d",
                        value["data"],
                        "/f",
                    ],
                    check=True,
                )
        for name, existed in state.values["firewall"].items():
            if not existed:
                self.runner.run(
                    [
                        powershell,
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        (
                            "$r=Get-NetFirewallRule -Name $env:PTP_RULE "
                            "-ErrorAction SilentlyContinue;"
                            "if($null -ne $r){$r|Remove-NetFirewallRule "
                            "-ErrorAction Stop}"
                        ),
                    ],
                    check=True,
                    env={"PTP_RULE": name},
                )
        self.runner.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    "Set-NetAdapterAdvancedProperty -Name $env:PTP_INTERFACE "
                    "-RegistryKeyword '*PtpHardwareTimestamp' "
                    "-RegistryValue $env:PTP_ADAPTER_VALUE -NoRestart -ErrorAction Stop;"
                    "Restart-NetAdapter -Name $env:PTP_INTERFACE -Confirm:$false "
                    "-ErrorAction Stop"
                ),
            ],
            check=True,
            timeout=60,
            env={
                "PTP_INTERFACE": str(state.values["interface"]),
                "PTP_ADAPTER_VALUE": str(state.values["adapter_value"]),
            },
        )
        if state.values["service_running"]:
            self._restart_w32time()

    @staticmethod
    def _default_state_path() -> str:
        root = os.environ.get("ProgramData", r"C:\ProgramData")
        return str(Path(root) / "ptp-sync" / "windows-ptp-state.json")


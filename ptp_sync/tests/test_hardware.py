from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ptp_sync.hardware import open_backend
from ptp_sync.hardware import linuxptp as linuxptp_module
from ptp_sync.hardware.linuxptp import (
    LinuxPtpBackend,
    parse_ethtool_timestamping,
    parse_hwstamp_policy,
    parse_phc2sys_servo,
    parse_pmc_offset,
)
from ptp_sync.hardware.models import BackendStatus, HardwareError, HardwareRequest
from ptp_sync.hardware.runner import CommandResult
from ptp_sync.hardware.state import StateStore, TransactionState
from ptp_sync.hardware.windows_ptp import (
    _TimestampCaps,
    _hardware_ipv4,
    parse_reg_query,
)


class _FakeRunner:
    def which(self, executable: str) -> str:
        return f"/usr/bin/{executable}"

    def run(self, args, **_kwargs) -> CommandResult:
        command = tuple(str(arg) for arg in args)
        stdout = ""
        if command[:2] == ("systemctl", "is-active"):
            stdout = "inactive"
        elif command[:2] == ("systemctl", "is-enabled"):
            stdout = "disabled"
        elif command[0].endswith("ethtool"):
            stdout = """
Capabilities:
 hardware-transmit
 hardware-receive
 hardware-raw-clock
PTP Hardware Clock: 0
Hardware Transmit Timestamp Modes: HWTSTAMP_TX_ON
Hardware Receive Filter Modes: HWTSTAMP_FILTER_PTP_V2_L4_EVENT
"""
        elif command[0].endswith(("ptp4l", "phc2sys", "pmc")):
            stdout = "4.4"
        elif command[0].endswith("hwstamp_ctl"):
            stdout = "tx_type 0\nrx_filter 0"
        return CommandResult(command, 0, stdout.strip(), "")


class _TestLinuxBackend(LinuxPtpBackend):
    def _resolve_interface(self, requested: str | None) -> str | None:
        return requested or "eth0"

    def status(self, request: HardwareRequest) -> BackendStatus:
        return BackendStatus(self.name, True, "locked")


class HardwareParserTests(unittest.TestCase):
    def test_ethtool_hardware_capabilities(self) -> None:
        output = """
Time stamping parameters for eth0:
Capabilities:
        hardware-transmit
        hardware-receive
        hardware-raw-clock
PTP Hardware Clock: 2
Hardware Transmit Timestamp Modes:
        HWTSTAMP_TX_ON
Hardware Receive Filter Modes:
        HWTSTAMP_FILTER_PTP_V2_L4_EVENT
"""
        parsed = parse_ethtool_timestamping(output)
        self.assertTrue(parsed["hardware_transmit"])
        self.assertTrue(parsed["hardware_receive"])
        self.assertTrue(parsed["hardware_raw_clock"])
        self.assertEqual(parsed["phc_index"], 2)
        self.assertTrue(parsed["rx_ptpv2"])

    def test_hwstamp_and_pmc_parsers(self) -> None:
        self.assertEqual(
            parse_hwstamp_policy("tx_type 1\nrx_filter 12\n"), (1, 12)
        )
        self.assertEqual(parse_pmc_offset("offsetFromMaster -314"), -314)
        self.assertEqual(
            parse_phc2sys_servo(
                "CLOCK_REALTIME phc offset -20 s1 freq +3\n"
                "CLOCK_REALTIME phc offset 47 s2 freq +1"
            ),
            (47, "s2"),
        )

    def test_windows_registry_parser_preserves_type_and_data(self) -> None:
        output = """
HKEY_LOCAL_MACHINE\\Example
    Enabled    REG_DWORD    0x1
    DllName    REG_EXPAND_SZ    %SystemRoot%\\System32\\ptpprov.dll
"""
        parsed = parse_reg_query(output)
        self.assertEqual(parsed["Enabled"]["type"], "REG_DWORD")
        self.assertEqual(
            parsed["DllName"]["data"], r"%SystemRoot%\System32\ptpprov.dll"
        )

    def test_windows_ipv4_capability_requires_rx_and_tx(self) -> None:
        caps = _TimestampCaps()
        caps.hardware.ipv4_event_rx = 1
        self.assertFalse(_hardware_ipv4(caps))
        caps.hardware.tagged_tx = 1
        self.assertTrue(_hardware_ipv4(caps))


class HardwareConfigurationTests(unittest.TestCase):
    def test_linux_slave_clock_direction(self) -> None:
        backend = LinuxPtpBackend()
        request = HardwareRequest(role="slave", interface="eth0", domain=3)
        config = backend._render_config(request, "eth0")
        unit = backend._render_phc2sys_unit(request, "eth0")
        self.assertIn("time_stamping            hardware", config)
        self.assertIn("domainNumber             3", config)
        self.assertIn("-s eth0 -c CLOCK_REALTIME", unit)

    def test_linux_master_clock_direction_and_time_properties(self) -> None:
        backend = LinuxPtpBackend()
        request = HardwareRequest(
            role="master", interface="eth0", domain=0, utc_offset=37
        )
        config = backend._render_config(request, "eth0")
        phc = backend._render_phc2sys_unit(request, "eth0")
        ptp = backend._render_ptp4l_unit(request, "eth0")
        self.assertRegex(config, r"(?:masterOnly|serverOnly)\s+1")
        self.assertIn("-s CLOCK_REALTIME -c eth0", phc)
        self.assertIn("currentUtcOffset 37", ptp)
        self.assertIn("currentUtcOffsetValid 1", ptp)

    def test_transaction_state_round_trip_and_remove(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "state.json")
            state = TransactionState(
                "linuxptp", values={"service": {"active": True}}, completed=True
            )
            store.save(state)
            self.assertTrue(store.exists())
            loaded = store.load()
            self.assertEqual(loaded.backend, "linuxptp")
            self.assertTrue(loaded.completed)
            self.assertTrue(loaded.values["service"]["active"])
            store.remove()
            self.assertFalse(store.exists())

    def test_backend_auto_selects_current_platform(self) -> None:
        backend = open_backend("auto")
        self.assertIn(backend.name, ("linuxptp", "windows-ptp"))

    def test_linux_apply_is_idempotent_and_restore_removes_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "ptp4l.conf"
            ptp_unit = root / "ptp-sync-ptp4l.service"
            phc_unit = root / "ptp-sync-phc2sys.service"
            state = root / "state.json"
            request = HardwareRequest(
                role="slave",
                interface="eth0",
                state_file=str(state),
                assume_yes=True,
            )
            backend = _TestLinuxBackend(_FakeRunner())  # type: ignore[arg-type]
            with (
                mock.patch.object(linuxptp_module, "CONFIG_PATH", config),
                mock.patch.object(linuxptp_module, "PTP4L_UNIT", ptp_unit),
                mock.patch.object(linuxptp_module, "PHC2SYS_UNIT", phc_unit),
                mock.patch.object(linuxptp_module.os, "geteuid", return_value=0),
            ):
                result = backend.apply(request)
                self.assertTrue(result.healthy)
                self.assertTrue(state.exists())
                with self.assertRaises(HardwareError):
                    backend.apply(request)
                restored = backend.restore(request)
                self.assertTrue(restored.healthy)
                self.assertFalse(state.exists())
                self.assertFalse(config.exists())


if __name__ == "__main__":
    unittest.main()


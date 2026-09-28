from __future__ import annotations

import unittest

from ptp_sync.gui_model import (
    SoftwareSettings,
    build_software_command,
    parse_log_sample,
)


class GuiCommandTests(unittest.TestCase):
    def test_build_slave_command(self) -> None:
        settings = SoftwareSettings(
            role="slave",
            bind="127.0.0.1",
            master="192.0.2.10",
            domain=3,
            peer_event_port=319,
            peer_general_port=320,
            apply_clock=True,
            verbose=True,
        )
        command = build_software_command(settings, "/usr/bin/python3")
        self.assertEqual(command[:5], ["/usr/bin/python3", "-u", "-m", "ptp_sync", "software"])
        self.assertIn("slave", command)
        self.assertIn("--master", command)
        self.assertIn("192.0.2.10", command)
        self.assertIn("--apply", command)
        self.assertIn("--verbose", command)
        self.assertNotIn("--interval", command)

    def test_build_master_command(self) -> None:
        settings = SoftwareSettings(
            role="master",
            peer="192.0.2.20",
            interval=0.25,
            multicast=True,
            standard_ports=True,
        )
        command = build_software_command(settings, "python")
        self.assertIn("--peer", command)
        self.assertIn("--interval", command)
        self.assertIn("0.25", command)
        self.assertIn("--multicast", command)
        self.assertIn("--standard-ports", command)
        self.assertNotIn("--master", command)
        self.assertNotIn("--apply", command)

    def test_invalid_filter_settings_are_rejected(self) -> None:
        settings = SoftwareSettings(
            role="slave",
            master="192.0.2.10",
            warmup=9,
            window=8,
        )
        with self.assertRaisesRegex(ValueError, "warmup"):
            build_software_command(settings)

    def test_non_finite_timing_values_are_rejected(self) -> None:
        for field, value in (
            ("interval", float("nan")),
            ("duration", float("inf")),
            ("step_threshold_us", float("-inf")),
        ):
            settings = SoftwareSettings(role="slave", master="192.0.2.10")
            setattr(settings, field, value)
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    build_software_command(settings)


class GuiLogParserTests(unittest.TestCase):
    def test_parse_slave_sample(self) -> None:
        sample = parse_log_sample(
            "12:00:00 INFO ptp.slave: seq=12 offset=+183.4 us "
            "delay=241.0 us filtered=+176.2 us n=12"
        )
        self.assertIsNotNone(sample)
        assert sample is not None
        self.assertEqual(sample.sequence, 12)
        self.assertAlmostEqual(sample.offset_us, 183.4)
        self.assertAlmostEqual(sample.filtered_us, 176.2)
        self.assertAlmostEqual(sample.delay_us, 241.0)
        self.assertEqual(sample.count, 12)

    def test_non_sample_log_is_ignored(self) -> None:
        self.assertIsNone(parse_log_sample("grandmaster duration elapsed"))


if __name__ == "__main__":
    unittest.main()


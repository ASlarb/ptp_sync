from __future__ import annotations

import contextlib
import io
import select
import socket
import time
import unittest

from ptp_sync.cli import main
from ptp_sync.clock import ClockAdjustment, SystemClock
from ptp_sync.protocol import MessageType, PtpError, make_header, unpack_message
from ptp_sync.slave import PtpSlave, SyncSample
from ptp_sync.transport import PtpSockets


class _FakeClock(SystemClock):
    def __init__(self) -> None:
        self.applied: list[int] = []

    def now_ns(self) -> int:
        return 0

    def step_ns(self, delta_ns: int) -> ClockAdjustment:
        return ClockAdjustment(True, "fake-step", delta_ns)

    def slew_ns(self, offset_ns: int) -> ClockAdjustment:
        return ClockAdjustment(True, "fake-slew", offset_ns)

    def apply_offset(
        self, offset_ns: int, *, step_threshold_ns: int = 500_000
    ) -> ClockAdjustment:
        self.applied.append(offset_ns)
        return ClockAdjustment(True, "fake", offset_ns)


class SoftwareServoTests(unittest.TestCase):
    def test_adjustment_clears_pre_correction_filter_window(self) -> None:
        clock = _FakeClock()
        slave = PtpSlave(
            clock,
            object(),  # type: ignore[arg-type]
            "127.0.0.1",
            apply_clock=True,
            warmup=2,
            window=4,
        )
        slave._pending_t1[99] = 1
        slave._pending_t2[99] = 2
        slave._awaiting_t4[99] = (1, 2, 3)
        for sequence, offset in ((1, 1_000), (2, 1_200)):
            sample = SyncSample(sequence, 0, 0, 0, 0, offset, 100)
            slave.samples.append(sample)
            slave._filter_offsets.append(offset)
            slave._handle_sample(sample)
        self.assertEqual(clock.applied, [1_100])
        self.assertEqual(len(slave._filter_offsets), 0)
        self.assertEqual(slave._pending_t1, {})
        self.assertEqual(slave._pending_t2, {})
        self.assertEqual(slave._awaiting_t4, {})

        sample = SyncSample(3, 0, 0, 0, 0, 20, 100)
        slave.samples.append(sample)
        slave._filter_offsets.append(sample.offset_ns)
        slave._handle_sample(sample)
        self.assertEqual(clock.applied, [1_100])

    def test_warmup_cannot_exceed_filter_window(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(
                    [
                        "slave",
                        "--master",
                        "127.0.0.1",
                        "--warmup",
                        "9",
                        "--window",
                        "8",
                    ]
                )


class TransportRobustnessTests(unittest.TestCase):
    def test_type_underlength_message_is_rejected(self) -> None:
        header = make_header(
            MessageType.FOLLOW_UP,
            b"\x01\x02\x03\x04\x05\x06\x07\x08",
            1,
        )
        header.message_length = 34
        with self.assertRaises(PtpError):
            unpack_message(header.pack())

    def test_malformed_datagram_is_dropped(self) -> None:
        sockets = PtpSockets(
            bind_addr="127.0.0.1",
            event_port=0,
            general_port=0,
            multicast=False,
            interface_addr=None,
            clock_now=time.time_ns,
        )
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sender.sendto(b"not-a-ptp-message", sockets.event.getsockname())
            readable, _, _ = select.select([sockets.event], [], [], 1.0)
            self.assertTrue(readable)
            self.assertIsNone(sockets.recv("event"))
        finally:
            sender.close()
            sockets.close()


if __name__ == "__main__":
    unittest.main()


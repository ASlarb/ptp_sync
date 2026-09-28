"""PTPv2 slave. Typically run on Windows; also works on Linux."""

from __future__ import annotations

import logging
import select
import socket
import statistics
import time
from collections import deque
from dataclasses import dataclass

from .clock import ClockAdjustment, ClockError, SystemClock, clock_identity, identity_hex
from .protocol import MessageType, PtpMessage, make_header, offset_and_delay_ns
from .transport import PtpSockets

log = logging.getLogger("ptp.slave")


@dataclass
class SyncSample:
    sequence: int
    t1: int
    t2: int
    t3: int
    t4: int
    offset_ns: int
    delay_ns: int


class PtpSlave:
    def __init__(
        self,
        clock: SystemClock,
        socks: PtpSockets,
        master_host: str,
        *,
        domain: int = 0,
        apply_clock: bool = False,
        step_threshold_ns: int = 500_000,
        warmup: int = 4,
        window: int = 8,
        delay_req_interval: float = 1.0,
        duration: float | None = None,
    ) -> None:
        self.clock = clock
        self.socks = socks
        self.master_host = master_host
        try:
            self.master_addr = socket.gethostbyname(master_host)
        except OSError:
            self.master_addr = master_host
        self.domain = domain
        self.apply_clock = apply_clock
        self.step_threshold_ns = step_threshold_ns
        self.warmup = warmup
        self.window = window
        self.delay_req_interval = delay_req_interval
        self.duration = duration
        self.identity = clock_identity()
        self.samples: deque[SyncSample] = deque(maxlen=256)
        self._filter_offsets: deque[int] = deque(maxlen=max(1, window))
        self._pending_t2: dict[int, int] = {}
        self._pending_t1: dict[int, int] = {}
        self._awaiting_t4: dict[int, tuple[int, int, int]] = {}
        self._master_port_identity: tuple[bytes, int] | None = None
        self._next_announce = 0.0
        log.info(
            "slave identity=%s master=%s apply=%s",
            identity_hex(self.identity),
            master_host,
            apply_clock,
        )

    def run(self) -> None:
        self._send_delay_req(sequence=0)
        deadline = (
            time.monotonic() + self.duration if self.duration is not None else None
        )
        try:
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    log.info("slave duration elapsed")
                    self._log_summary()
                    return
                timeout = max(0.0, self._next_announce - time.monotonic())
                if deadline is not None:
                    timeout = min(timeout, max(0.0, deadline - time.monotonic()))
                readable, _, _ = select.select(self.socks.filenos(), [], [], timeout)
                if self.socks.event.fileno() in readable:
                    self._on_event()
                if self.socks.general.fileno() in readable:
                    self._on_general()
                if time.monotonic() >= self._next_announce:
                    # Keep the master aware of this slave even before first Sync.
                    self._send_delay_req(sequence=0)
        except KeyboardInterrupt:
            log.info("slave stopped")
            self._log_summary()
        finally:
            self.clock.restore()
            self.socks.close()

    def _on_event(self) -> None:
        rec = self.socks.recv("event")
        if rec is None:
            return
        if rec.addr[0] != self.master_addr:
            log.warning("ignoring event packet from unexpected host %s", rec.addr[0])
            return
        msg = rec.message
        if msg.header.domain_number != self.domain:
            return
        if msg.message_type != MessageType.SYNC:
            return
        if not self._accept_master_identity(
            msg.header.clock_identity, msg.header.port_number
        ):
            return
        seq = msg.header.sequence_id
        log.debug("Sync seq=%d kernel_ts=%s", seq, rec.kernel_timestamp)
        self._pending_t2[seq] = rec.ingress_ns
        t1 = msg.origin_timestamp_ns
        if t1:
            self._pending_t1[seq] = t1
        self._try_start_delay(seq)

    def _on_general(self) -> None:
        rec = self.socks.recv("general")
        if rec is None:
            return
        if rec.addr[0] != self.master_addr:
            log.warning("ignoring general packet from unexpected host %s", rec.addr[0])
            return
        msg = rec.message
        if msg.header.domain_number != self.domain:
            return
        seq = msg.header.sequence_id
        if msg.message_type == MessageType.FOLLOW_UP:
            if not self._accept_master_identity(
                msg.header.clock_identity, msg.header.port_number
            ):
                return
            self._pending_t1[seq] = msg.origin_timestamp_ns
            self._try_start_delay(seq)
        elif msg.message_type == MessageType.DELAY_RESP:
            if not self._accept_master_identity(
                msg.header.clock_identity, msg.header.port_number
            ):
                return
            if (
                msg.requesting_clock_identity != self.identity
                or msg.requesting_port_number != 1
            ):
                log.warning("ignoring Delay_Resp not addressed to this slave")
                return
            pending = self._awaiting_t4.pop(seq, None)
            if pending is None:
                return
            t1, t2, t3 = pending
            t4 = msg.origin_timestamp_ns
            offset, delay = offset_and_delay_ns(t1, t2, t3, t4)
            sample = SyncSample(seq, t1, t2, t3, t4, offset, delay)
            self.samples.append(sample)
            self._filter_offsets.append(sample.offset_ns)
            self._handle_sample(sample)

    def _try_start_delay(self, seq: int) -> None:
        if seq not in self._pending_t1 or seq not in self._pending_t2:
            return
        t1 = self._pending_t1.pop(seq)
        t2 = self._pending_t2.pop(seq)
        t3 = self._send_delay_req(sequence=seq)
        self._awaiting_t4[seq] = (t1, t2, t3)

    def _send_delay_req(self, sequence: int) -> int:
        t3 = self.clock.now_ns()
        req = PtpMessage(
            header=make_header(
                MessageType.DELAY_REQ,
                self.identity,
                sequence,
                domain_number=self.domain,
                two_step=False,
            ),
            origin_timestamp_ns=t3,
        ).pack()
        self.socks.send_event(req, self.master_host)
        self._next_announce = time.monotonic() + self.delay_req_interval
        return t3

    def _handle_sample(self, sample: SyncSample) -> None:
        filtered = self._filtered_offset()
        delay_us = sample.delay_ns / 1000.0
        offset_us = sample.offset_ns / 1000.0
        filt_us = (filtered / 1000.0) if filtered is not None else float("nan")
        log.info(
            "seq=%d offset=%+.1f us delay=%.1f us filtered=%+.1f us n=%d",
            sample.sequence,
            offset_us,
            delay_us,
            filt_us,
            len(self.samples),
        )
        if not self.apply_clock:
            return
        if len(self._filter_offsets) < self.warmup:
            log.info(
                "warmup %d/%d, clock not applied yet",
                len(self._filter_offsets),
                self.warmup,
            )
            return
        if filtered is None:
            return
        try:
            adj = self.clock.apply_offset(
                filtered, step_threshold_ns=self.step_threshold_ns
            )
        except ClockError as exc:
            log.error("failed to apply clock: %s", exc)
            return
        self._log_adjustment(adj)
        # Never re-apply a phase estimate containing samples from before the
        # preceding correction. Re-warm the servo using the corrected clock.
        self._filter_offsets.clear()
        self._pending_t1.clear()
        self._pending_t2.clear()
        self._awaiting_t4.clear()

    def _filtered_offset(self) -> int | None:
        if not self._filter_offsets:
            return None
        recent = list(self._filter_offsets)
        if len(recent) == 1:
            return recent[0]
        return int(statistics.median(recent))

    def _accept_master_identity(self, identity: bytes, port_number: int) -> bool:
        port_identity = (identity, port_number)
        if self._master_port_identity is None:
            self._master_port_identity = port_identity
            return True
        if port_identity != self._master_port_identity:
            log.warning(
                "ignoring packet from unexpected master identity=%s/%d",
                identity_hex(identity),
                port_number,
            )
            return False
        return True

    def _log_adjustment(self, adj: ClockAdjustment) -> None:
        log.info(
            "applied %s %+d ns (%s)",
            adj.method,
            adj.requested_offset_ns,
            adj.detail,
        )

    def _log_summary(self) -> None:
        if not self.samples:
            log.warning("no completed PTP exchanges")
            return
        offsets = [s.offset_ns for s in self.samples]
        delays = [s.delay_ns for s in self.samples]
        log.info(
            "summary samples=%d offset median=%+.1f us p95=%+.1f us  "
            "delay median=%.1f us",
            len(self.samples),
            statistics.median(offsets) / 1000.0,
            _percentile(offsets, 95) / 1000.0,
            statistics.median(delays) / 1000.0,
        )


def _percentile(values: list[int], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac

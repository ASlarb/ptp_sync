"""PTPv2 two-step grandmaster. Typically run on Linux."""

from __future__ import annotations

import logging
import select
import time
from dataclasses import dataclass, field

from .clock import SystemClock, clock_identity, identity_hex
from .protocol import MessageType, PtpMessage, make_header
from .transport import PtpSockets

log = logging.getLogger("ptp.master")


@dataclass
class SlaveRecord:
    host: str
    event_port: int
    general_port: int
    last_seen: float = field(default_factory=time.monotonic)


class PtpMaster:
    def __init__(
        self,
        clock: SystemClock,
        socks: PtpSockets,
        *,
        domain: int = 0,
        sync_interval: float = 1.0,
        peer: str | None = None,
        multicast: bool = False,
        mcast_group: str = "224.0.1.129",
        duration: float | None = None,
    ) -> None:
        self.clock = clock
        self.socks = socks
        self.domain = domain
        self.sync_interval = sync_interval
        self.multicast = multicast
        self.mcast_group = mcast_group
        self.duration = duration
        self.identity = clock_identity()
        self.sequence = 0
        self.slaves: dict[str, SlaveRecord] = {}
        if peer:
            self.slaves[peer] = SlaveRecord(
                peer, socks.peer_event_port, socks.peer_general_port
            )
        log.info(
            "grandmaster identity=%s domain=%d listen=%d/%d peer=%d/%d",
            identity_hex(self.identity),
            domain,
            socks.event_port,
            socks.general_port,
            socks.peer_event_port,
            socks.peer_general_port,
        )

    def run(self) -> None:
        next_sync = time.monotonic()
        deadline = (
            time.monotonic() + self.duration if self.duration is not None else None
        )
        try:
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    log.info("grandmaster duration elapsed")
                    return
                timeout = max(0.0, next_sync - time.monotonic())
                if deadline is not None:
                    timeout = min(timeout, max(0.0, deadline - time.monotonic()))
                readable, _, _ = select.select(self.socks.filenos(), [], [], timeout)
                if self.socks.event.fileno() in readable:
                    self._on_event()
                if self.socks.general.fileno() in readable:
                    self._on_general()
                now = time.monotonic()
                if now >= next_sync:
                    self._send_sync()
                    next_sync = now + self.sync_interval
        except KeyboardInterrupt:
            log.info("grandmaster stopped")
        finally:
            self.socks.close()

    def _targets(self) -> list[SlaveRecord]:
        if self.multicast:
            return [
                SlaveRecord(
                    self.mcast_group,
                    self.socks.peer_event_port,
                    self.socks.peer_general_port,
                )
            ]
        stale = time.monotonic() - 15.0
        return [rec for rec in self.slaves.values() if rec.last_seen >= stale]

    def _send_sync(self) -> None:
        targets = self._targets()
        if not targets:
            log.debug("no slaves registered yet; waiting for Delay_Req")
            return
        self.sequence = (self.sequence + 1) & 0xFFFF
        t1 = self.clock.now_ns()
        sync = PtpMessage(
            header=make_header(
                MessageType.SYNC,
                self.identity,
                self.sequence,
                domain_number=self.domain,
                two_step=True,
            ),
            origin_timestamp_ns=0,
        ).pack()
        follow = PtpMessage(
            header=make_header(
                MessageType.FOLLOW_UP,
                self.identity,
                self.sequence,
                domain_number=self.domain,
                two_step=False,
            ),
            origin_timestamp_ns=t1,
        ).pack()
        for rec in targets:
            self.socks.send_event(sync, rec.host, rec.event_port)
            self.socks.send_general(follow, rec.host, rec.general_port)
        log.info(
            "Sync seq=%d t1=%d ns -> %s",
            self.sequence,
            t1,
            ", ".join(f"{r.host}:{r.event_port}" for r in targets),
        )

    def _on_event(self) -> None:
        rec = self.socks.recv("event")
        if rec is None:
            return
        msg = rec.message
        if msg.message_type != MessageType.DELAY_REQ:
            return
        if msg.header.domain_number != self.domain:
            return
        t4 = rec.ingress_ns
        host = rec.addr[0]
        self.slaves[host] = SlaveRecord(
            host, self.socks.peer_event_port, self.socks.peer_general_port
        )
        resp = PtpMessage(
            header=make_header(
                MessageType.DELAY_RESP,
                self.identity,
                msg.header.sequence_id,
                domain_number=self.domain,
                two_step=False,
            ),
            origin_timestamp_ns=t4,
            requesting_clock_identity=msg.header.clock_identity,
            requesting_port_number=msg.header.port_number,
        ).pack()
        self.socks.send_general(resp, host)
        log.info(
            "Delay_Resp seq=%d t4=%d ns slave=%s kernel_ts=%s",
            msg.header.sequence_id,
            t4,
            host,
            rec.kernel_timestamp,
        )

    def _on_general(self) -> None:
        self.socks.recv("general")

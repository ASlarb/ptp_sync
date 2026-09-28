"""UDP transport for PTP event (Sync/Delay_Req) and general (Follow_Up/Delay_Resp) messages."""

from __future__ import annotations

import socket
import struct
import sys
from dataclasses import dataclass

from .protocol import PTP_PRIMARY_MCAST, PtpError, PtpMessage, unpack_message


@dataclass(slots=True)
class Received:
    message: PtpMessage
    addr: tuple[str, int]
    ingress_ns: int
    kernel_timestamp: bool


class PtpSockets:
    def __init__(
        self,
        *,
        bind_addr: str,
        event_port: int,
        general_port: int,
        multicast: bool,
        interface_addr: str | None,
        clock_now,
        peer_event_port: int | None = None,
        peer_general_port: int | None = None,
    ) -> None:
        self.event_port = event_port
        self.general_port = general_port
        self.peer_event_port = peer_event_port if peer_event_port is not None else event_port
        self.peer_general_port = (
            peer_general_port if peer_general_port is not None else general_port
        )
        self._now = clock_now
        self.event = _make_udp(bind_addr, event_port, multicast, interface_addr)
        self.general = _make_udp(bind_addr, general_port, multicast, interface_addr)
        self._use_kernel_ts = sys.platform.startswith("linux")
        if self._use_kernel_ts:
            _enable_timestampns(self.event)
            _enable_timestampns(self.general)

    def filenos(self) -> list[int]:
        return [self.event.fileno(), self.general.fileno()]

    def send_event(self, payload: bytes, host: str, port: int | None = None) -> None:
        self.event.sendto(payload, (host, port if port is not None else self.peer_event_port))

    def send_general(self, payload: bytes, host: str, port: int | None = None) -> None:
        dest = port if port is not None else self.peer_general_port
        self.general.sendto(payload, (host, dest))

    def recv(self, which: str) -> Received | None:
        sock = self.event if which == "event" else self.general
        try:
            if self._use_kernel_ts:
                return _recv_linux(sock, self._now)
            data, addr = sock.recvfrom(2048)
            ingress = self._now()
            return Received(unpack_message(data), (addr[0], addr[1]), ingress, False)
        except (BlockingIOError, InterruptedError):
            return None
        except (OSError, PtpError, ValueError):
            return None

    def close(self) -> None:
        self.event.close()
        self.general.close()


def _make_udp(
    bind_addr: str, port: int, multicast: bool, interface_addr: str | None
) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind_addr, port))
    sock.setblocking(False)
    if multicast:
        iface = interface_addr or bind_addr
        if iface in ("0.0.0.0", ""):
            iface = "0.0.0.0"
        mreq = socket.inet_aton(PTP_PRIMARY_MCAST) + socket.inet_aton(iface)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        ttl = struct.pack("b", 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
        if interface_addr:
            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_MULTICAST_IF,
                socket.inet_aton(interface_addr),
            )
    return sock


def _enable_timestampns(sock: socket.socket) -> None:
    # SO_TIMESTAMPNS = 35 on Linux.
    try:
        sock.setsockopt(socket.SOL_SOCKET, 35, 1)
    except OSError:
        pass


def _recv_linux(sock: socket.socket, clock_now) -> Received | None:
    try:
        data, ancdata, _flags, addr = sock.recvmsg(2048, 1024)
    except (BlockingIOError, InterruptedError):
        return None
    ingress = clock_now()
    kernel_ns = _ancillary_timestamp_ns(ancdata)
    kernel = kernel_ns is not None
    if kernel_ns is not None:
        ingress = kernel_ns
    return Received(unpack_message(data), (addr[0], addr[1]), ingress, kernel)


def _ancillary_timestamp_ns(ancdata) -> int | None:
    scm_timestampns = 35
    for level, typ, data in ancdata:
        if level == socket.SOL_SOCKET and typ == scm_timestampns and len(data) >= 16:
            sec, nsec = struct.unpack("qq", data[:16])
            return int(sec) * 1_000_000_000 + int(nsec)
    return None

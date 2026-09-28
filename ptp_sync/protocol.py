"""IEEE 1588-2008 (PTPv2) message encode/decode for a two-step unicast clock."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

HEADER_SIZE = 34
TIMESTAMP_SIZE = 10
PORT_IDENTITY_SIZE = 10

PTP_VERSION = 2
PTP_PRIMARY_MCAST = "224.0.1.129"


class MessageType(IntEnum):
    SYNC = 0x0
    DELAY_REQ = 0x1
    FOLLOW_UP = 0x8
    DELAY_RESP = 0x9


class ControlField(IntEnum):
    SYNC = 0x00
    DELAY_REQ = 0x01
    FOLLOW_UP = 0x02
    DELAY_RESP = 0x03


FLAG_TWO_STEP = 0x0200


class PtpError(ValueError):
    pass


def timestamp_from_ns(time_ns: int) -> bytes:
    if time_ns < 0:
        raise PtpError(f"negative timestamp: {time_ns}")
    seconds, nanoseconds = divmod(time_ns, 1_000_000_000)
    return seconds.to_bytes(6, "big") + nanoseconds.to_bytes(4, "big")


def timestamp_to_ns(raw: bytes) -> int:
    if len(raw) != TIMESTAMP_SIZE:
        raise PtpError(f"timestamp must be {TIMESTAMP_SIZE} bytes, got {len(raw)}")
    seconds = int.from_bytes(raw[:6], "big")
    nanoseconds = int.from_bytes(raw[6:], "big")
    if nanoseconds >= 1_000_000_000:
        raise PtpError(f"invalid nanoseconds field: {nanoseconds}")
    return seconds * 1_000_000_000 + nanoseconds


def pack_port_identity(clock_identity: bytes, port_number: int) -> bytes:
    if len(clock_identity) != 8:
        raise PtpError("clock identity must be 8 bytes")
    return clock_identity + port_number.to_bytes(2, "big")


def unpack_port_identity(raw: bytes) -> tuple[bytes, int]:
    if len(raw) != PORT_IDENTITY_SIZE:
        raise PtpError(f"port identity must be {PORT_IDENTITY_SIZE} bytes")
    return raw[:8], int.from_bytes(raw[8:], "big")


@dataclass(slots=True)
class PtpHeader:
    message_type: MessageType
    message_length: int
    domain_number: int
    flags: int
    correction_ns: int
    clock_identity: bytes
    port_number: int
    sequence_id: int
    control: ControlField
    log_message_interval: int
    transport_specific: int = 0

    def pack(self) -> bytes:
        if len(self.clock_identity) != 8:
            raise PtpError("clock identity must be 8 bytes")
        first = ((self.transport_specific & 0xF) << 4) | (int(self.message_type) & 0xF)
        version = PTP_VERSION & 0xF
        # correctionField is nanoseconds left-shifted by 16.
        correction = int(self.correction_ns) << 16
        return struct.pack(
            "!BBHBBHQI8sHHBb",
            first,
            version,
            self.message_length,
            self.domain_number & 0xFF,
            0,
            self.flags & 0xFFFF,
            correction & 0xFFFFFFFFFFFFFFFF,
            0,
            self.clock_identity,
            self.port_number & 0xFFFF,
            self.sequence_id & 0xFFFF,
            int(self.control) & 0xFF,
            self.log_message_interval,
        )


def unpack_header(raw: bytes) -> PtpHeader:
    if len(raw) < HEADER_SIZE:
        raise PtpError(f"PTP header too short: {len(raw)} bytes")
    (
        first,
        version,
        message_length,
        domain_number,
        _reserved,
        flags,
        correction,
        _reserved2,
        clock_identity,
        port_number,
        sequence_id,
        control,
        log_message_interval,
    ) = struct.unpack("!BBHBBHQI8sHHBb", raw[:HEADER_SIZE])
    if (version & 0xF) != PTP_VERSION:
        raise PtpError(f"unsupported PTP version: {version & 0xF}")
    return PtpHeader(
        message_type=MessageType(first & 0xF),
        message_length=message_length,
        domain_number=domain_number,
        flags=flags,
        correction_ns=correction >> 16,
        clock_identity=clock_identity,
        port_number=port_number,
        sequence_id=sequence_id,
        control=ControlField(control),
        log_message_interval=log_message_interval,
        transport_specific=(first >> 4) & 0xF,
    )


@dataclass(slots=True)
class PtpMessage:
    header: PtpHeader
    origin_timestamp_ns: int = 0
    requesting_clock_identity: bytes | None = None
    requesting_port_number: int = 1

    @property
    def message_type(self) -> MessageType:
        return self.header.message_type

    def pack(self) -> bytes:
        body = timestamp_from_ns(self.origin_timestamp_ns)
        if self.header.message_type == MessageType.DELAY_RESP:
            if self.requesting_clock_identity is None:
                raise PtpError("Delay_Resp requires requestingPortIdentity")
            body += pack_port_identity(
                self.requesting_clock_identity, self.requesting_port_number
            )
        self.header.message_length = HEADER_SIZE + len(body)
        return self.header.pack() + body


def unpack_message(raw: bytes) -> PtpMessage:
    header = unpack_header(raw)
    minimum_length = HEADER_SIZE + TIMESTAMP_SIZE
    if header.message_type == MessageType.DELAY_RESP:
        minimum_length += PORT_IDENTITY_SIZE
    if header.message_length < minimum_length:
        raise PtpError(
            f"{header.message_type.name} message too short: "
            f"{header.message_length}, expected at least {minimum_length}"
        )
    if len(raw) < header.message_length:
        raise PtpError(
            f"truncated PTP message: got {len(raw)}, expected {header.message_length}"
        )
    payload = raw[HEADER_SIZE : header.message_length]
    origin_ns = 0
    requesting_clock = None
    requesting_port = 1
    if len(payload) >= TIMESTAMP_SIZE:
        origin_ns = timestamp_to_ns(payload[:TIMESTAMP_SIZE])
    if header.message_type == MessageType.DELAY_RESP:
        if len(payload) < TIMESTAMP_SIZE + PORT_IDENTITY_SIZE:
            raise PtpError("Delay_Resp missing requestingPortIdentity")
        requesting_clock, requesting_port = unpack_port_identity(
            payload[TIMESTAMP_SIZE : TIMESTAMP_SIZE + PORT_IDENTITY_SIZE]
        )
    return PtpMessage(
        header=header,
        origin_timestamp_ns=origin_ns,
        requesting_clock_identity=requesting_clock,
        requesting_port_number=requesting_port,
    )


def make_header(
    message_type: MessageType,
    clock_identity: bytes,
    sequence_id: int,
    *,
    domain_number: int = 0,
    port_number: int = 1,
    two_step: bool = True,
    log_message_interval: int = 0,
) -> PtpHeader:
    control = {
        MessageType.SYNC: ControlField.SYNC,
        MessageType.DELAY_REQ: ControlField.DELAY_REQ,
        MessageType.FOLLOW_UP: ControlField.FOLLOW_UP,
        MessageType.DELAY_RESP: ControlField.DELAY_RESP,
    }[message_type]
    flags = FLAG_TWO_STEP if two_step and message_type == MessageType.SYNC else 0
    return PtpHeader(
        message_type=message_type,
        message_length=HEADER_SIZE,
        domain_number=domain_number,
        flags=flags,
        correction_ns=0,
        clock_identity=clock_identity,
        port_number=port_number,
        sequence_id=sequence_id,
        control=control,
        log_message_interval=log_message_interval,
    )


def offset_and_delay_ns(t1: int, t2: int, t3: int, t4: int) -> tuple[int, int]:
    """Return (offset_ns, delay_ns) using the PTP two-way formula.

    offset = ((t2 - t1) - (t4 - t3)) / 2
    delay  = ((t2 - t1) + (t4 - t3)) / 2

    offset is slave - master. To align the slave, subtract offset from the
    slave clock (or step it by -offset).
    """
    t2_t1 = t2 - t1
    t4_t3 = t4 - t3
    offset = (t2_t1 - t4_t3) // 2
    delay = (t2_t1 + t4_t3) // 2
    return offset, delay

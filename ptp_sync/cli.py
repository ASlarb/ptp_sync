"""CLI for software PTP clock alignment between Linux and Windows hosts."""

from __future__ import annotations

import argparse
import logging
import sys

from .clock import clock_identity, identity_hex, open_clock
from .hardware.cli import add_hardware_parser, run_hardware
from .master import PtpMaster
from .protocol import (
    MessageType,
    PtpMessage,
    make_header,
    offset_and_delay_ns,
    unpack_message,
)
from .slave import PtpSlave
from .transport import PtpSockets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Software IEEE 1588v2 (PTP) two-step clock sync. "
            "Run the master on one host and the slave on the other."
        )
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    _add_master_args(
        sub.add_parser("master", help="run software PTP grandmaster (compatibility alias)")
    )
    _add_slave_args(
        sub.add_parser(
            "slave", help="run software PTP slave (compatibility alias)"
        )
    )
    software = sub.add_parser(
        "software", help="run the Python software-timestamp fallback"
    )
    software_sub = software.add_subparsers(dest="software_cmd", required=True)
    _add_master_args(software_sub.add_parser("master", help="run software master"))
    _add_slave_args(software_sub.add_parser("slave", help="run software slave"))
    add_hardware_parser(sub)
    sub.add_parser("selftest", help="encode/decode and offset formula checks")
    args = parser.parse_args(argv)
    _setup_logging(args.verbose if hasattr(args, "verbose") else False)
    if args.cmd == "hardware":
        return run_hardware(args)
    if args.cmd == "selftest":
        return _selftest()
    command = args.software_cmd if args.cmd == "software" else args.cmd
    clock = open_clock()
    event_port, general_port = _ports(args)
    if command == "slave" and args.warmup > args.window:
        parser.error("--warmup must be less than or equal to --window")
    socks = PtpSockets(
        bind_addr=args.bind,
        event_port=event_port,
        general_port=general_port,
        multicast=args.multicast,
        interface_addr=args.interface,
        clock_now=clock.now_ns,
        peer_event_port=args.peer_event_port,
        peer_general_port=args.peer_general_port,
    )
    if command == "master":
        PtpMaster(
            clock,
            socks,
            domain=args.domain,
            sync_interval=args.interval,
            peer=args.peer,
            multicast=args.multicast,
            duration=args.duration,
        ).run()
        return 0
    PtpSlave(
        clock,
        socks,
        master_host=args.master,
        domain=args.domain,
        apply_clock=args.apply,
        step_threshold_ns=int(args.step_threshold_us * 1000),
        warmup=args.warmup,
        window=args.window,
        duration=args.duration,
    ).run()
    return 0


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--bind", default="0.0.0.0", help="local bind address")
    p.add_argument("--interface", default=None, help="interface IP for multicast")
    p.add_argument("--domain", type=_domain, default=0, help="PTP domain number")
    p.add_argument(
        "--standard-ports",
        action="store_true",
        help="use IEEE ports 319/320 (requires root/admin on most systems)",
    )
    p.add_argument(
        "--event-port", type=_port, default=31900, help="local event listen port"
    )
    p.add_argument(
        "--general-port", type=_port, default=32000, help="local general listen port"
    )
    p.add_argument(
        "--peer-event-port",
        type=_port,
        default=None,
        help="other host event port (default: same as --event-port)",
    )
    p.add_argument(
        "--peer-general-port",
        type=_port,
        default=None,
        help="other host general port (default: same as --general-port)",
    )
    p.add_argument("--multicast", action="store_true", help="use 224.0.1.129")
    p.add_argument(
        "--duration", type=_positive_float, default=None, help="stop after N seconds"
    )
    p.add_argument("-v", "--verbose", action="store_true")


def _add_master_args(p: argparse.ArgumentParser) -> None:
    _add_common(p)
    p.add_argument(
        "--peer",
        default=None,
        help="Windows slave IPv4. Optional: slave also registers via Delay_Req",
    )
    p.add_argument(
        "--interval", type=_positive_float, default=1.0, help="Sync period in seconds"
    )


def _add_slave_args(p: argparse.ArgumentParser) -> None:
    _add_common(p)
    p.add_argument("--master", required=True, help="grandmaster IPv4 (Linux host)")
    p.add_argument(
        "--apply",
        action="store_true",
        help="write the measured offset into the OS clock (needs root/Administrator)",
    )
    p.add_argument(
        "--step-threshold-us",
        type=_positive_float,
        default=500.0,
        help="step the clock when |offset| exceeds this many microseconds",
    )
    p.add_argument(
        "--warmup", type=_positive_int, default=4, help="exchanges before --apply"
    )
    p.add_argument(
        "--window", type=_positive_int, default=8, help="median filter window"
    )


def _ports(args: argparse.Namespace) -> tuple[int, int]:
    if args.standard_ports:
        return 319, 320
    return args.event_port, args.general_port


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return value


def _positive_float(raw: str) -> float:
    value = float(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return value


def _port(raw: str) -> int:
    value = int(raw)
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError("port must be in 1..65535")
    return value


def _domain(raw: str) -> int:
    value = int(raw)
    if not 0 <= value <= 127:
        raise argparse.ArgumentTypeError("domain must be in 0..127")
    return value


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _selftest() -> int:
    identity = clock_identity()
    header = make_header(MessageType.FOLLOW_UP, identity, 7, domain_number=1)
    msg = PtpMessage(header=header, origin_timestamp_ns=1_700_000_000_123_456_789)
    raw = msg.pack()
    back = unpack_message(raw)
    assert back.header.sequence_id == 7
    assert back.header.domain_number == 1
    assert back.origin_timestamp_ns == 1_700_000_000_123_456_789
    assert back.header.clock_identity == identity

    delay = PtpMessage(
        header=make_header(MessageType.DELAY_RESP, identity, 9),
        origin_timestamp_ns=42,
        requesting_clock_identity=identity,
        requesting_port_number=1,
    )
    delay_back = unpack_message(delay.pack())
    assert delay_back.origin_timestamp_ns == 42
    assert delay_back.requesting_clock_identity == identity

    # Symmetric 200 us path delay, slave 1.5 ms ahead of master.
    t1 = 0
    t2 = 1_500_000 + 200_000
    t3 = 2_000_000
    t4 = 2_000_000 - 1_500_000 + 200_000
    offset, path = offset_and_delay_ns(t1, t2, t3, t4)
    assert offset == 1_500_000, offset
    assert path == 200_000, path
    print(f"selftest ok  clock-identity={identity_hex(identity)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

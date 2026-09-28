"""CLI integration for hardware PTP backends."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass
from typing import Any

from . import open_backend
from .models import HardwareError, HardwareRequest


def add_hardware_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "hardware",
        help="manage NIC hardware timestamp PTP through the operating-system stack",
    )
    actions = parser.add_subparsers(dest="hardware_action", required=True)
    for action in ("detect", "plan", "apply", "status", "restore"):
        command = actions.add_parser(action)
        _add_common(command)
        if action == "apply":
            command.add_argument(
                "--yes",
                action="store_true",
                help="confirm service changes and possible network-adapter restart",
            )


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--backend",
        choices=("auto", "linuxptp", "windows-ptp"),
        default="auto",
    )
    parser.add_argument("--interface", help="network interface/NIC name")
    parser.add_argument("--role", choices=("slave", "master"), default="slave")
    parser.add_argument(
        "--master",
        dest="masters",
        action="append",
        default=[],
        help="allowed Grandmaster IPv4; repeat for multiple addresses",
    )
    parser.add_argument("--domain", type=int, default=0)
    parser.add_argument(
        "--utc-offset",
        type=int,
        default=None,
        help="current TAI-UTC offset; required for Linux master",
    )
    parser.add_argument("--state-file", default=None)
    parser.add_argument("--json", action="store_true", dest="json_output")


def run_hardware(args: argparse.Namespace) -> int:
    request = HardwareRequest(
        backend=args.backend,
        role=args.role,
        interface=args.interface,
        masters=list(args.masters),
        domain=args.domain,
        utc_offset=args.utc_offset,
        state_file=args.state_file,
        assume_yes=bool(getattr(args, "yes", False)),
    )
    try:
        backend = open_backend(args.backend)
        if args.hardware_action == "detect":
            result = backend.detect(request)
            exit_code = 0 if result.supported else 2
        elif args.hardware_action == "plan":
            result = backend.plan(request)
            exit_code = 0 if result.applicable else 2
        elif args.hardware_action == "apply":
            result = backend.apply(request)
            exit_code = 0 if result.healthy else 3
        elif args.hardware_action == "status":
            result = backend.status(request)
            exit_code = 0 if result.healthy else 3
        elif args.hardware_action == "restore":
            result = backend.restore(request)
            exit_code = 0 if result.healthy else 3
        else:
            raise HardwareError(f"unknown hardware action: {args.hardware_action}")
    except HardwareError as exc:
        if args.json_output:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        else:
            print(f"hardware PTP error: {exc}")
        return 2
    _print_result(result, args.json_output)
    return exit_code


def _print_result(value: Any, as_json: bool) -> None:
    payload = asdict(value) if is_dataclass(value) else value
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if "supported" in payload:
        print(f"backend={payload['backend']} supported={payload['supported']}")
    elif "applicable" in payload:
        print(
            f"backend={payload['backend']} role={payload['role']} "
            f"applicable={payload['applicable']}"
        )
    else:
        print(
            f"backend={payload['backend']} state={payload['state']} "
            f"healthy={payload['healthy']}"
        )
    for check in payload.get("checks", []):
        marker = "ok" if check["ok"] else "FAIL"
        print(f"  [{marker}] {check['name']}: {check['detail']}")
    for change in payload.get("changes", []):
        disruptive = " disruptive" if change["disruptive"] else ""
        print(
            f"  [{change['action']}{disruptive}] {change['target']}: "
            f"{change['detail']}"
        )
    for detail in payload.get("details", []):
        print(f"  {detail}")
    for warning in payload.get("warnings", []):
        print(f"  warning: {warning}")


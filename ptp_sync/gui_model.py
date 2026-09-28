"""GUI-independent validation, command construction, and log parsing."""

from __future__ import annotations

import math
import re
import sys
from dataclasses import dataclass


@dataclass(slots=True)
class SoftwareSettings:
    role: str = "slave"
    bind: str = "0.0.0.0"
    interface: str | None = None
    peer: str | None = None
    master: str | None = None
    domain: int = 0
    event_port: int = 31900
    general_port: int = 32000
    peer_event_port: int | None = None
    peer_general_port: int | None = None
    interval: float = 1.0
    duration: float | None = None
    apply_clock: bool = False
    step_threshold_us: float = 500.0
    warmup: int = 4
    window: int = 8
    multicast: bool = False
    standard_ports: bool = False
    verbose: bool = False


@dataclass(frozen=True, slots=True)
class LogSample:
    sequence: int
    offset_us: float
    delay_us: float
    filtered_us: float
    count: int


_SAMPLE_RE = re.compile(
    r"\bseq=(?P<sequence>\d+)\s+"
    r"offset=(?P<offset>[+-]?\d+(?:\.\d+)?)\s+us\s+"
    r"delay=(?P<delay>[+-]?\d+(?:\.\d+)?)\s+us\s+"
    r"filtered=(?P<filtered>[+-]?\d+(?:\.\d+)?)\s+us\s+"
    r"n=(?P<count>\d+)\b"
)


def validate_software_settings(settings: SoftwareSettings) -> None:
    if settings.role not in ("master", "slave"):
        raise ValueError("role must be master or slave")
    if not settings.bind:
        raise ValueError("bind address is required")
    if settings.role == "slave" and not settings.master:
        raise ValueError("slave mode requires a master address")
    if not 0 <= settings.domain <= 127:
        raise ValueError("domain must be in 0..127")
    for name, value in (
        ("event port", settings.event_port),
        ("general port", settings.general_port),
        ("peer event port", settings.peer_event_port),
        ("peer general port", settings.peer_general_port),
    ):
        if value is not None and not 1 <= value <= 65535:
            raise ValueError(f"{name} must be in 1..65535")
    if not math.isfinite(settings.interval) or settings.interval <= 0:
        raise ValueError("interval must be greater than zero")
    if settings.duration is not None and (
        not math.isfinite(settings.duration) or settings.duration <= 0
    ):
        raise ValueError("duration must be greater than zero")
    if (
        not math.isfinite(settings.step_threshold_us)
        or settings.step_threshold_us <= 0
    ):
        raise ValueError("step threshold must be greater than zero")
    if settings.warmup <= 0 or settings.window <= 0:
        raise ValueError("warmup and window must be greater than zero")
    if settings.warmup > settings.window:
        raise ValueError("warmup must be less than or equal to window")


def build_software_command(
    settings: SoftwareSettings, python_executable: str | None = None
) -> list[str]:
    validate_software_settings(settings)
    command = [
        python_executable or sys.executable,
        "-u",
        "-m",
        "ptp_sync",
        "software",
        settings.role,
        "--bind",
        settings.bind,
        "--domain",
        str(settings.domain),
        "--event-port",
        str(settings.event_port),
        "--general-port",
        str(settings.general_port),
    ]
    if settings.interface:
        command.extend(["--interface", settings.interface])
    if settings.peer_event_port is not None:
        command.extend(["--peer-event-port", str(settings.peer_event_port)])
    if settings.peer_general_port is not None:
        command.extend(["--peer-general-port", str(settings.peer_general_port)])
    if settings.duration is not None:
        command.extend(["--duration", _number(settings.duration)])
    if settings.multicast:
        command.append("--multicast")
    if settings.standard_ports:
        command.append("--standard-ports")
    if settings.verbose:
        command.append("--verbose")

    if settings.role == "master":
        if settings.peer:
            command.extend(["--peer", settings.peer])
        command.extend(["--interval", _number(settings.interval)])
    else:
        command.extend(
            [
                "--master",
                str(settings.master),
                "--step-threshold-us",
                _number(settings.step_threshold_us),
                "--warmup",
                str(settings.warmup),
                "--window",
                str(settings.window),
            ]
        )
        if settings.apply_clock:
            command.append("--apply")
    return command


def parse_log_sample(line: str) -> LogSample | None:
    match = _SAMPLE_RE.search(line)
    if match is None:
        return None
    return LogSample(
        sequence=int(match.group("sequence")),
        offset_us=float(match.group("offset")),
        delay_us=float(match.group("delay")),
        filtered_us=float(match.group("filtered")),
        count=int(match.group("count")),
    )


def _number(value: float) -> str:
    return f"{value:g}"


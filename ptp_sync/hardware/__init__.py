"""System-managed hardware timestamp PTP backends."""

from __future__ import annotations

import sys

from .base import HardwareBackend
from .linuxptp import LinuxPtpBackend
from .models import HardwareError
from .runner import Runner
from .windows_ptp import WindowsPtpBackend

__all__ = [
    "HardwareBackend",
    "HardwareError",
    "LinuxPtpBackend",
    "WindowsPtpBackend",
    "open_backend",
]


def open_backend(name: str, runner: Runner | None = None) -> HardwareBackend:
    selected = name
    if selected == "auto":
        if sys.platform.startswith("linux"):
            selected = "linuxptp"
        elif sys.platform == "win32":
            selected = "windows-ptp"
        else:
            raise HardwareError(f"no hardware PTP backend for {sys.platform}")
    if selected == "linuxptp":
        return LinuxPtpBackend(runner)
    if selected == "windows-ptp":
        return WindowsPtpBackend(runner)
    raise HardwareError(f"unknown hardware PTP backend: {name}")


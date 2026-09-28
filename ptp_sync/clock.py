"""Cross-platform CLOCK_REALTIME read / step / slew for Linux and Windows."""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import uuid
from dataclasses import dataclass

log = logging.getLogger("ptp.clock")


class ClockError(RuntimeError):
    pass


def clock_identity() -> bytes:
    """Build an 8-byte EUI-64-style clock identity from the host MAC."""
    mac = uuid.getnode().to_bytes(6, "big")
    return mac[:3] + b"\xff\xfe" + mac[3:]


def identity_hex(identity: bytes) -> str:
    return identity.hex(":")


@dataclass(frozen=True)
class ClockAdjustment:
    applied: bool
    method: str
    requested_offset_ns: int
    remaining_offset_ns: int = 0
    detail: str = ""


class SystemClock:
    """Read and optionally correct the OS realtime clock."""

    def now_ns(self) -> int:
        raise NotImplementedError

    def step_ns(self, delta_ns: int) -> ClockAdjustment:
        raise NotImplementedError

    def slew_ns(self, offset_ns: int) -> ClockAdjustment:
        raise NotImplementedError

    def restore(self) -> None:
        return

    def apply_offset(
        self,
        offset_ns: int,
        *,
        step_threshold_ns: int = 500_000,
    ) -> ClockAdjustment:
        """Align local time to master by removing `offset_ns` (slave - master)."""
        correction = -int(offset_ns)
        if abs(offset_ns) >= step_threshold_ns:
            return self.step_ns(correction)
        return self.slew_ns(correction)


class LinuxClock(SystemClock):
    CLOCK_REALTIME = 0
    ADJ_OFFSET = 0x0001
    ADJ_NANO = 0x2000
    ADJ_SETOFFSET = 0x0100

    def __init__(self) -> None:
        self._libc = ctypes.CDLL("libc.so.6", use_errno=True)
        self._timespec = _Timespec
        self._timex = _Timex
        self._libc.clock_gettime.argtypes = [ctypes.c_int, ctypes.POINTER(_Timespec)]
        self._libc.clock_gettime.restype = ctypes.c_int
        self._libc.clock_settime.argtypes = [ctypes.c_int, ctypes.POINTER(_Timespec)]
        self._libc.clock_settime.restype = ctypes.c_int
        self._libc.clock_adjtime.argtypes = [ctypes.c_int, ctypes.POINTER(_Timex)]
        self._libc.clock_adjtime.restype = ctypes.c_int

    def now_ns(self) -> int:
        ts = _Timespec()
        if self._libc.clock_gettime(self.CLOCK_REALTIME, ctypes.byref(ts)) != 0:
            raise ClockError(f"clock_gettime failed: {os.strerror(ctypes.get_errno())}")
        return int(ts.tv_sec) * 1_000_000_000 + int(ts.tv_nsec)

    def step_ns(self, delta_ns: int) -> ClockAdjustment:
        target = self.now_ns() + int(delta_ns)
        ts = _Timespec()
        ts.tv_sec = target // 1_000_000_000
        ts.tv_nsec = target % 1_000_000_000
        if self._libc.clock_settime(self.CLOCK_REALTIME, ctypes.byref(ts)) != 0:
            raise ClockError(
                "clock_settime failed "
                f"({os.strerror(ctypes.get_errno())}). Run as root, or omit --apply."
            )
        return ClockAdjustment(True, "clock_settime", delta_ns, 0, "step")

    def slew_ns(self, offset_ns: int) -> ClockAdjustment:
        tx = _Timex()
        tx.modes = self.ADJ_OFFSET | self.ADJ_NANO
        tx.offset = int(offset_ns)
        rc = self._libc.clock_adjtime(self.CLOCK_REALTIME, ctypes.byref(tx))
        if rc >= 0:
            return ClockAdjustment(True, "clock_adjtime", offset_ns, 0, f"status={rc}")
        raise ClockError(
            "clock_adjtime slew failed "
            f"({os.strerror(ctypes.get_errno())}); clock was not stepped"
        )


class WindowsClock(SystemClock):
    EPOCH_DIFF_100NS = 116444736000000000

    def __init__(self) -> None:
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        self._kernel32.GetSystemTimePreciseAsFileTime.argtypes = [
            ctypes.POINTER(_FileTime)
        ]
        self._kernel32.GetSystemTimePreciseAsFileTime.restype = None
        self._ntdll.NtSetSystemTime.argtypes = [
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
        ]
        self._ntdll.NtSetSystemTime.restype = ctypes.c_long
        self._kernel32.GetSystemTimeAdjustment.argtypes = [
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_int),
        ]
        self._kernel32.GetSystemTimeAdjustment.restype = ctypes.c_int
        self._kernel32.SetSystemTimeAdjustment.argtypes = [
            ctypes.c_uint32,
            ctypes.c_int,
        ]
        self._kernel32.SetSystemTimeAdjustment.restype = ctypes.c_int
        self._saved_adjustment: int | None = None
        self._saved_disabled: int | None = None
        self._restore_timer: threading.Timer | None = None
        self._adjustment_lock = threading.RLock()
        self._slew_generation = 0

    def now_ns(self) -> int:
        ft = _FileTime()
        self._kernel32.GetSystemTimePreciseAsFileTime(ctypes.byref(ft))
        value = (int(ft.dwHighDateTime) << 32) | int(ft.dwLowDateTime)
        return (value - self.EPOCH_DIFF_100NS) * 100

    def step_ns(self, delta_ns: int) -> ClockAdjustment:
        target = self.now_ns() + int(delta_ns)
        ft = _unix_ns_to_filetime(target)
        status = self._ntdll.NtSetSystemTime(ctypes.byref(ft), None)
        if status != 0:
            raise ClockError(
                f"NtSetSystemTime failed (NTSTATUS=0x{status & 0xFFFFFFFF:08X}). "
                "Run this process as Administrator, or omit --apply."
            )
        return ClockAdjustment(True, "NtSetSystemTime", delta_ns, 0, "step")

    def slew_ns(self, offset_ns: int) -> ClockAdjustment:
        with self._adjustment_lock:
            adjustment = ctypes.c_uint32()
            increment = ctypes.c_uint32()
            disabled = ctypes.c_int()
            if not self._kernel32.GetSystemTimeAdjustment(
                ctypes.byref(adjustment), ctypes.byref(increment), ctypes.byref(disabled)
            ):
                raise ClockError(
                    f"GetSystemTimeAdjustment failed (err={ctypes.get_last_error()})"
                )
            if self._saved_adjustment is None:
                self._saved_adjustment = int(adjustment.value)
                self._saved_disabled = int(disabled.value)
            period_100ns = int(increment.value) or 156250
            # Spread the remaining offset over ~1 second of timer interrupts.
            ticks_per_sec = max(1, 10_000_000 // period_100ns)
            delta_per_tick = int(round(offset_ns / 100 / ticks_per_sec))
            new_adjustment = period_100ns + delta_per_tick
            if new_adjustment <= 0:
                new_adjustment = 1
            if not self._kernel32.SetSystemTimeAdjustment(new_adjustment, 0):
                raise ClockError(
                    f"SetSystemTimeAdjustment failed (err={ctypes.get_last_error()}). "
                    "Run as Administrator, or omit --apply."
                )
            self._slew_generation += 1
            generation = self._slew_generation
            if self._restore_timer is not None:
                self._restore_timer.cancel()
            self._restore_timer = threading.Timer(
                1.0, self._finish_slew, args=(generation,)
            )
            self._restore_timer.daemon = True
            self._restore_timer.start()
        return ClockAdjustment(
            True,
            "SetSystemTimeAdjustment",
            offset_ns,
            0,
            f"increment={period_100ns} adjustment={new_adjustment}",
        )

    def restore(self) -> None:
        with self._adjustment_lock:
            self._slew_generation += 1
            if self._restore_timer is not None:
                self._restore_timer.cancel()
                self._restore_timer = None
            self._finish_slew()

    def _finish_slew(self, generation: int | None = None) -> None:
        with self._adjustment_lock:
            if generation is not None and generation != self._slew_generation:
                return
            if self._saved_adjustment is None:
                return
            if not self._kernel32.SetSystemTimeAdjustment(
                self._saved_adjustment, self._saved_disabled or 0
            ):
                log.error(
                    "failed to restore Windows time adjustment (err=%d)",
                    ctypes.get_last_error(),
                )
                return
            self._saved_adjustment = None
            self._saved_disabled = None
            self._restore_timer = None


class _Timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_int64), ("tv_nsec", ctypes.c_int64)]


class _Timex(ctypes.Structure):
    _fields_ = [
        ("modes", ctypes.c_uint),
        ("offset", ctypes.c_long),
        ("freq", ctypes.c_long),
        ("maxerror", ctypes.c_long),
        ("esterror", ctypes.c_long),
        ("status", ctypes.c_int),
        ("constant", ctypes.c_long),
        ("precision", ctypes.c_long),
        ("tolerance", ctypes.c_long),
        ("time", _Timespec),
        ("tick", ctypes.c_long),
        ("ppsfreq", ctypes.c_long),
        ("jitter", ctypes.c_long),
        ("shift", ctypes.c_int),
        ("stabil", ctypes.c_long),
        ("jitcnt", ctypes.c_long),
        ("calcnt", ctypes.c_long),
        ("errcnt", ctypes.c_long),
        ("stbcnt", ctypes.c_long),
        ("tai", ctypes.c_int),
        ("_pad", ctypes.c_int32 * 11),
    ]


class _FileTime(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", ctypes.c_uint32),
        ("dwHighDateTime", ctypes.c_uint32),
    ]


def _unix_ns_to_filetime(time_ns: int) -> _FileTime:
    value = time_ns // 100 + WindowsClock.EPOCH_DIFF_100NS
    ft = _FileTime()
    ft.dwLowDateTime = value & 0xFFFFFFFF
    ft.dwHighDateTime = value >> 32
    return ft


def open_clock() -> SystemClock:
    if sys.platform.startswith("linux"):
        return LinuxClock()
    if sys.platform == "win32":
        return WindowsClock()
    raise ClockError(f"unsupported platform: {sys.platform}")

"""Shared data models for system-managed hardware PTP backends."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class HardwareRequest:
    backend: str = "auto"
    role: str = "slave"
    interface: str | None = None
    masters: list[str] = field(default_factory=list)
    domain: int = 0
    utc_offset: int | None = None
    state_file: str | None = None
    assume_yes: bool = False


@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


@dataclass(slots=True)
class Detection:
    backend: str
    platform: str
    supported: bool
    checks: list[Check] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PlannedChange:
    action: str
    target: str
    detail: str
    disruptive: bool = False


@dataclass(slots=True)
class ChangePlan:
    backend: str
    role: str
    applicable: bool
    checks: list[Check] = field(default_factory=list)
    changes: list[PlannedChange] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    generated: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class BackendStatus:
    backend: str
    healthy: bool
    state: str
    details: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class HardwareError(RuntimeError):
    """Raised when a hardware backend cannot safely perform an operation."""


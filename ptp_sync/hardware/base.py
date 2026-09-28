"""Backend contract for hardware PTP orchestration."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import BackendStatus, ChangePlan, Detection, HardwareRequest


class HardwareBackend(ABC):
    name: str

    @abstractmethod
    def detect(self, request: HardwareRequest) -> Detection:
        raise NotImplementedError

    @abstractmethod
    def plan(self, request: HardwareRequest) -> ChangePlan:
        raise NotImplementedError

    @abstractmethod
    def apply(self, request: HardwareRequest) -> BackendStatus:
        raise NotImplementedError

    @abstractmethod
    def status(self, request: HardwareRequest) -> BackendStatus:
        raise NotImplementedError

    @abstractmethod
    def restore(self, request: HardwareRequest) -> BackendStatus:
        raise NotImplementedError


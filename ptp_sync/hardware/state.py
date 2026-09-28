"""Transaction snapshots for reversible hardware PTP configuration."""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .models import HardwareError


@dataclass(slots=True)
class TransactionState:
    backend: str
    created_unix: float = field(default_factory=time.time)
    values: dict[str, Any] = field(default_factory=dict)
    owned_paths: list[str] = field(default_factory=list)
    completed: bool = False


class StateStore:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> TransactionState:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return TransactionState(**data)
        except (OSError, ValueError, TypeError) as exc:
            raise HardwareError(f"cannot load transaction state {self.path}: {exc}") from exc

    def save(self, state: TransactionState) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                dir=str(self.path.parent),
                text=True,
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(asdict(state), stream, indent=2, sort_keys=True)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            except BaseException:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
        except OSError as exc:
            raise HardwareError(f"cannot save transaction state {self.path}: {exc}") from exc

    def remove(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            raise HardwareError(f"cannot remove transaction state {self.path}: {exc}") from exc


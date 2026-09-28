"""Injectable process runner used by hardware PTP backends."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Mapping, Sequence

from .models import HardwareError


@dataclass(frozen=True, slots=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Runner:
    """Run commands without a command shell so arguments cannot be reinterpreted."""

    def which(self, executable: str) -> str | None:
        return shutil.which(executable)

    def run(
        self,
        args: Sequence[str],
        *,
        check: bool = False,
        timeout: float = 30.0,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        if not args:
            raise ValueError("command must not be empty")
        command = [str(arg) for arg in args]
        process_env = None
        if env is not None:
            process_env = os.environ.copy()
            process_env.update({str(key): str(value) for key, value in env.items()})
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=process_env,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise HardwareError(f"failed to run {command[0]}: {exc}") from exc
        result = CommandResult(
            tuple(command),
            completed.returncode,
            completed.stdout.strip(),
            completed.stderr.strip(),
        )
        if check and not result.ok:
            message = result.stderr or result.stdout or "unknown error"
            raise HardwareError(
                f"command failed ({result.returncode}): {' '.join(command)}: {message}"
            )
        return result


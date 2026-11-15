from __future__ import annotations

import hashlib
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_identity: str
    executable: str
    argv_fingerprint: str
    command_line: str


def process_identity(pid: int, executable: str, argv: list[str]) -> ProcessIdentity:
    """Capture a best-effort process signature without passing task secrets on argv."""
    fingerprint = hashlib.sha256("\0".join([executable, *argv]).encode()).hexdigest()
    if os.uname().sysname == "Darwin":
        result = subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        parts = result.stdout.strip().split(None, 5)
        if result.returncode != 0 or len(parts) < 6:
            raise RuntimeError("Could not confirm child process start time and command")
        start = " ".join(parts[:5])
        command_line = parts[5]
        if Path(executable).name not in command_line:
            raise RuntimeError("Child process command does not match the configured executable")
    else:
        start = f"observed:{time.time_ns()}"
        command_line = " ".join([executable, *argv])
    return ProcessIdentity(pid, start, executable, fingerprint, command_line)


def verify_process_identity(pid: int, executable: str, start_identity: str, stored_command: str) -> bool | None:
    """True means identity matched; False means absent; None means the PID is ambiguous."""
    if pid <= 0:
        return False
    if not executable or not start_identity or not stored_command:
        return None
    if os.uname().sysname != "Darwin":
        return None
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return False
    parts = result.stdout.strip().split(None, 5)
    if len(parts) < 6:
        return None
    actual_start = " ".join(parts[:5])
    actual_command = parts[5]
    if actual_start != start_identity:
        return None
    if Path(executable).name not in actual_command or Path(executable).name not in stored_command:
        return None
    return True

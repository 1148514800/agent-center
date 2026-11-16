from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from agent_center.storage.redaction import StreamingRedactor


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout_bytes: int
    stderr_bytes: int
    truncated: bool
    timed_out: bool
    cancelled: bool


class ProcessRunner:
    """Manage Agent CLI lifetimes and logs. Callers must never pass project commands here."""

    def __init__(self, *, termination_grace_seconds: int = 5, max_log_bytes: int = 52_428_800):
        self.grace = termination_grace_seconds
        self.max_log_bytes = max_log_bytes

    async def run(
        self,
        executable: str,
        argv: list[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        stdin: bytes,
        timeout: float,
        stdout_path: Path,
        stderr_path: Path,
        on_started: Callable[[int], None] | None = None,
    ) -> ProcessResult:
        stdout_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        process = await asyncio.create_subprocess_exec(
            executable,
            *argv,
            cwd=str(cwd),
            env=dict(env),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=(os.name == "posix"),
        )
        assert process.stdout is not None and process.stderr is not None and process.stdin is not None
        stdin_writer = process.stdin
        secret_values = [
            value
            for name, value in env.items()
            if len(value) >= 4 and any(part in name.upper() for part in ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH"))
        ]
        redactors = {
            "stdout": StreamingRedactor(secret_values),
            "stderr": StreamingRedactor(secret_values),
        }
        counts = {"stdout": 0, "stderr": 0}
        total_written = 0
        truncated = False
        write_lock = asyncio.Lock()

        async def consume(stream: asyncio.StreamReader, path: Path, key: str) -> None:
            nonlocal total_written, truncated
            with path.open("wb") as output:
                os.chmod(path, 0o600)
                while chunk := await stream.read(65536):
                    counts[key] += len(chunk)
                    redacted = redactors[key].feed(chunk)
                    async with write_lock:
                        remaining = max(self.max_log_bytes - total_written, 0)
                        if remaining:
                            output.write(redacted[:remaining])
                            total_written += min(len(redacted), remaining)
                        if len(redacted) > remaining or redactors[key].truncated:
                            truncated = True
                tail = redactors[key].feed(b"", final=True)
                async with write_lock:
                    remaining = max(self.max_log_bytes - total_written, 0)
                    if remaining:
                        output.write(tail[:remaining])
                        total_written += min(len(tail), remaining)
                    if len(tail) > remaining or redactors[key].truncated:
                        truncated = True

        readers = [
            asyncio.create_task(consume(process.stdout, stdout_path, "stdout")),
            asyncio.create_task(consume(process.stderr, stderr_path, "stderr")),
        ]
        timed_out = False

        async def write_input() -> None:
            try:
                stdin_writer.write(stdin)
                await stdin_writer.drain()
            except BrokenPipeError, ConnectionResetError:
                # Child may exit before consuming input; process still goes through bounded reap.
                pass
            finally:
                stdin_writer.close()

        writer = asyncio.create_task(write_input())
        try:
            if on_started is not None:
                on_started(process.pid)
            try:
                await asyncio.wait_for(asyncio.gather(process.wait(), writer), timeout=timeout)
            except TimeoutError:
                timed_out = True
                await self._terminate(process)
                if not writer.done():
                    writer.cancel()
                    await asyncio.gather(writer, return_exceptions=True)
            if os.name == "posix" and process.pid is not None:
                await self._terminate_group(process)
            try:
                await asyncio.wait_for(asyncio.gather(*readers), timeout=self.grace)
            except TimeoutError:
                await self._terminate_group(process, force=True)
                for reader in readers:
                    reader.cancel()
                await asyncio.gather(*readers, return_exceptions=True)
                truncated = True
        except asyncio.CancelledError:
            await self._terminate_group(process, force=False)
            if not writer.done():
                writer.cancel()
            try:
                await asyncio.wait_for(asyncio.gather(*readers, return_exceptions=True), timeout=self.grace)
            except TimeoutError:
                await self._terminate_group(process, force=True)
                await asyncio.gather(*readers, return_exceptions=True)
            raise
        except BaseException:
            await self._terminate_group(process, force=True)
            await asyncio.gather(*readers, return_exceptions=True)
            raise
        finally:
            if process.returncode is None:
                await self._terminate(process)
            if writer and not writer.done():
                writer.cancel()
                await asyncio.gather(writer, return_exceptions=True)
        assert process.returncode is not None
        return ProcessResult(process.returncode, counts["stdout"], counts["stderr"], truncated, timed_out, False)

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=self.grace)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()

    async def _terminate_group(self, process: asyncio.subprocess.Process, *, force: bool = False) -> None:
        if os.name != "posix" or process.pid is None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            return
        if not force:
            deadline = asyncio.get_running_loop().time() + self.grace
            while asyncio.get_running_loop().time() < deadline:
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    return
                await asyncio.sleep(0.05)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)

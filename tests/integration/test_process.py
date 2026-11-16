from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from agent_center.runtime.process import ProcessRunner

pytestmark = pytest.mark.asyncio


async def test_process_reads_both_streams_without_deadlock(tmp_path: Path) -> None:
    out, err = tmp_path / "out", tmp_path / "err"
    script = "import sys\nfor _ in range(1000):\n sys.stdout.write('o'*200+'\\n')\n sys.stderr.write('e'*200+'\\n')\n"
    result = await ProcessRunner(max_log_bytes=600_000).run(
        sys.executable,
        ["-c", script],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "/bin")},
        stdin=b"",
        timeout=10,
        stdout_path=out,
        stderr_path=err,
    )
    assert result.returncode == 0
    assert result.stdout_bytes == 201_000
    assert result.stderr_bytes == 201_000
    assert not result.truncated


async def test_timeout_reaps_process_group(tmp_path: Path) -> None:
    out, err = tmp_path / "out", tmp_path / "err"
    script = (
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(30)"
    )
    result = await ProcessRunner(termination_grace_seconds=1).run(
        sys.executable,
        ["-c", script],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "/bin")},
        stdin=b"",
        timeout=0.3,
        stdout_path=out,
        stderr_path=err,
    )
    assert result.returncode != 0
    assert result.timed_out


async def test_group_child_that_ignores_term_does_not_hold_pipe_open(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("this process-group assertion is POSIX-only")
    out, err = tmp_path / "out", tmp_path / "err"
    child = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    parent = (
        "import signal,subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "print('ready',flush=True); time.sleep(30)"
    )
    result = await ProcessRunner(termination_grace_seconds=1).run(
        sys.executable,
        ["-c", parent],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "/bin")},
        stdin=b"",
        timeout=0.3,
        stdout_path=out,
        stderr_path=err,
    )
    assert result.timed_out
    assert result.returncode != 0
    assert result.stdout_bytes > 0


async def test_output_limit_marks_truncation(tmp_path: Path) -> None:
    out, err = tmp_path / "out", tmp_path / "err"
    result = await ProcessRunner(max_log_bytes=4096).run(
        sys.executable,
        ["-c", "print(('x'*100 + '\\n')*1000)"],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "/bin")},
        stdin=b"",
        timeout=10,
        stdout_path=out,
        stderr_path=err,
    )
    assert result.truncated
    assert len(out.read_bytes()) + len(err.read_bytes()) <= 4096


async def test_cancel_reaps_cli_process(tmp_path: Path) -> None:
    import asyncio

    ready = asyncio.Event()
    running = asyncio.create_task(
        ProcessRunner(termination_grace_seconds=1).run(
            sys.executable,
            ["-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            env={"PATH": "/usr/bin:/bin"},
            stdin=b"",
            timeout=40,
            stdout_path=tmp_path / "out",
            stderr_path=tmp_path / "err",
            on_started=lambda pid: ready.set(),
        )
    )
    await ready.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, timeout=5)

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from agent_center.capabilities import installation_identity
from agent_center.config import AppConfiguration
from agent_center.domain import TaskStatus
from agent_center.engine import TaskEngine
from agent_center.storage.database import Store
from agent_center.verification import VerificationError, VerificationService


@pytest.mark.parametrize("mode", ["good", "forged_text", "wrong_exit", "missing_terminal", "malformed"])
def test_verification_uses_independent_cli_run_and_does_not_execute_project_code(
    verified_config: AppConfiguration,
    store: Store,
    tmp_path: Path,
    mode: str,
) -> None:
    root = tmp_path / "workspace"
    (root / "tests").mkdir(parents=True)
    sentinel = tmp_path / "MUST_NOT_EXIST"
    (root / "tests/test_untrusted.py").write_text(f"from pathlib import Path\nPath({str(sentinel)!r}).touch()")
    argv = [*VerificationService.PYTEST_PREFIX, "tests/test_untrusted.py"]
    cli = tmp_path / "synthetic-cli"
    events = [
        {
            "type": "item.completed",
            "item": {
                "id": "cmd1",
                "type": "command_execution",
                "command": " ".join(argv),
                "cwd": str(root),
                "exit_code": 1 if mode == "wrong_exit" else 0,
                "status": "completed",
                "aggregated_output": "synthetic only",
            },
        },
        {"type": "turn.completed"},
    ]
    if mode == "forged_text":
        events[0] = {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "Tests all passed, exit code 0"},
        }
    if mode == "missing_terminal":
        events.pop()
    cli.write_text(
        f"#!{sys.executable}\nimport sys,time\nsys.stdin.read()\ntime.sleep(0.05)\n"
        + (
            "print('{invalid')\n"
            if mode == "malformed"
            else f"print({(''.join(json.dumps(e) + '\n' for e in events))!r},end='')\n"
        )
    )
    cli.chmod(0o700)
    agent = verified_config.agents["codex"].model_copy(update={"executable": str(cli)})
    caps = agent.capabilities.model_copy(
        update={"installation_identity": installation_identity(agent, verified_config.profiles["codex"])}
    )
    agent = agent.model_copy(update={"capabilities": caps})
    config = verified_config.model_copy(update={"agents": {"codex": agent}})
    engine = TaskEngine(config, store)
    task, _ = engine.create_task("Implement a feature", root)
    store.update_plan(
        task,
        [
            {
                "id": "s",
                "objective": "Implement feature",
                "acceptance_criteria": [
                    {"id": "c", "description": "Test passes", "verification_argv": argv},
                ],
            }
        ],
        1,
    )
    store.set_status(task, TaskStatus.VERIFYING)
    if mode in {"good", "wrong_exit"}:
        ids = asyncio.run(engine.verifier.run_for_criteria(task, 1, ["c"]))
        assert engine.verifier.is_fresh(task, ids) == (mode == "good")
        assert store.finish_if_current(task, 1, ids, ["c"]) == (mode == "good")
    else:
        with pytest.raises(VerificationError):
            asyncio.run(engine.verifier.run_for_criteria(task, 1, ["c"]))
        assert not store.evidence(task)[0]["complete"]
    assert len(store.runs(task)) == 1
    assert store.task(task)["agent_calls"] == 1
    assert not sentinel.exists()


@pytest.mark.asyncio
async def test_outer_cancellation_stops_verification_cli(
    verified_config: AppConfiguration,
    store: Store,
    tmp_path: Path,
) -> None:
    import os

    root = tmp_path / "workspace"
    (root / "tests").mkdir(parents=True)
    (root / "tests/test_cancel.py").write_text("raise RuntimeError('never run on host')")
    cli = tmp_path / "sleeping-cli"
    cli.write_text(f"#!{sys.executable}\nimport sys,time\nsys.stdin.read()\ntime.sleep(30)\n")
    cli.chmod(0o700)
    agent = verified_config.agents["codex"].model_copy(update={"executable": str(cli)})
    agent = agent.model_copy(
        update={
            "capabilities": agent.capabilities.model_copy(
                update={
                    "installation_identity": installation_identity(agent, verified_config.profiles["codex"]),
                }
            )
        }
    )
    config = verified_config.model_copy(update={"agents": {"codex": agent}})
    engine = TaskEngine(config, store)
    task_id, _ = engine.create_task("Implement cancellation fixture", root)
    store.update_plan(
        task_id,
        [
            {
                "id": "s",
                "objective": "Implement",
                "acceptance_criteria": [
                    {
                        "id": "c",
                        "description": "check",
                        "verification_argv": [*VerificationService.PYTEST_PREFIX, "tests/test_cancel.py"],
                    },
                ],
            }
        ],
        1,
    )
    running = asyncio.create_task(engine.verifier.run_for_criteria(task_id, 1, ["c"]))
    for _ in range(100):
        rows = store.runs(task_id)
        if rows and rows[0]["status"] == "running":
            break
        await asyncio.sleep(0.01)
    else:
        running.cancel()
        pytest.fail("Synthetic CLI never reached running state")
    pid = rows[0]["pid"]
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, timeout=8)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert store.runs(task_id)[0]["status"] == "cancelled"
    assert not store.evidence(task_id)

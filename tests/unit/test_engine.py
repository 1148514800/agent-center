from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from agent_center.adapters.base import AgentRunResult, LaunchSpec
from agent_center.config import AppConfiguration
from agent_center.domain import Criterion, Delegate, DirectAnswer, PlanStep, TaskStatus
from agent_center.engine import EngineBlocked, TaskEngine
from agent_center.runtime.process import ProcessResult
from agent_center.storage.database import Store


class FakeModel:
    def __init__(self, actions: list[Any]):
        self.actions = actions

    async def decide(self, **_: Any) -> Any:
        return self.actions.pop(0)

    async def answer_question(self, **_: Any) -> str:
        return "read only"


class PreflightAdapter:
    def __init__(self, *, fail: bool):
        self.fail = fail
        self.calls = 0

    def build_launch(self, request: Any) -> LaunchSpec:
        self.calls += 1
        if self.fail:
            raise RuntimeError("synthetic preflight failure")
        return LaunchSpec("/bin/true", (), {"PATH": "/usr/bin:/bin"}, b"", "synthetic")

    def parse(self, request: Any, process: ProcessResult) -> AgentRunResult:
        return AgentRunResult(process, (), True, None, "synthetic output")


class PreflightRunner:
    def __init__(self):
        self.calls = 0

    async def run(self, *args: Any, **kwargs: Any) -> ProcessResult:
        self.calls += 1
        out = kwargs["stdout_path"]
        err = kwargs["stderr_path"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("", encoding="utf-8")
        err.write_text("", encoding="utf-8")
        callback = kwargs.get("on_started")
        if callback:
            callback(99999999)
        return ProcessResult(0, 0, 0, False, False, False)


def test_active_deadline_uses_persisted_running_clock(config: AppConfiguration, store: Store, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_id, _ = TaskEngine(config, store, model_client=FakeModel([])).create_task("What is SQLite?", workspace)
    store.start_active_period(task_id)
    with store.transaction() as conn:
        conn.execute("UPDATE tasks SET active_started_at=? WHERE task_id=?", (time.time() - 100, task_id))
    config = config.model_copy(update={"limits": config.limits.model_copy(update={"max_active_seconds": 1})})
    engine = TaskEngine(config, store, model_client=FakeModel([]))
    result = asyncio.run(engine.run_task(task_id))
    assert result.startswith("blocked:")
    assert "active-time budget" in result
    assert store.task(task_id)["status"] == TaskStatus.BLOCKED
    assert store.task(task_id)["supervisor_calls"] == 0


def test_direct_answer_completes_without_agent_calls(config: AppConfiguration, store: Store, tmp_path: Path) -> None:
    model = FakeModel([DirectAnswer(action="answer", reason_summary="Simple question", answer="42")])
    engine = TaskEngine(config, store, model_client=model)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_id, _ = engine.create_task("What is 6x7?", workspace)
    assert asyncio.run(engine.run_task(task_id)) == "42"
    task = store.task(task_id)
    assert task["status"] == TaskStatus.SUCCEEDED
    assert task["agent_calls"] == 0


def test_external_publication_is_blocked_before_process_launch(
    config: AppConfiguration, store: Store, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_id, _ = TaskEngine(config, store, model_client=FakeModel([])).create_task("push the result", workspace)
    step = PlanStep(
        id="publish",
        objective="push the result",
        acceptance_criteria=[
            Criterion(
                id="publish-proof",
                description="unsupported",
                verification_argv=["python", "-m", "pytest", "-q", "tests/test_publish.py"],
            )
        ],
    )
    store.update_plan(task_id, [step.model_dump(mode="json")], 1)
    engine = TaskEngine(config, store, model_client=FakeModel([]))
    action = Delegate(
        action="delegate",
        reason_summary="policy gate",
        agent_id="codex",
        step_id=step.id,
        objective=step.objective,
        acceptance_criteria=step.acceptance_criteria,
    )
    with pytest.raises(EngineBlocked, match="External"):
        asyncio.run(engine._delegate(task_id, action, 1, "context"))
    assert store.runs(task_id) == []


def test_preflight_failure_routes_to_other_configured_agent_once(
    verified_config: AppConfiguration, store: Store, tmp_path: Path
) -> None:
    from agent_center.capabilities import installation_identity

    config = verified_config
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text("#!/bin/sh\necho synthetic\n", encoding="utf-8")
    fake_codex.chmod(0o700)
    config = config.model_copy(
        update={
            "agents": {
                "codex": config.agents["codex"].model_copy(
                    update={"executable": str(fake_codex), "allow_execution": True, "capability_status": "supported"}
                ),
                "deepseek": config.agents["deepseek"].model_copy(
                    update={"allow_execution": True, "capability_status": "supported"}
                ),
            },
            "limits": config.limits.model_copy(update={"max_step_retries": 1}),
        }
    )
    agent = config.agents["codex"]
    agent = agent.model_copy(
        update={
            "capabilities": agent.capabilities.model_copy(
                update={"installation_identity": installation_identity(agent, config.profiles[agent.default_profile])}
            )
        }
    )
    config = config.model_copy(update={"agents": {**config.agents, "codex": agent}})
    task_id, _ = TaskEngine(config, store, model_client=FakeModel([])).create_task(
        "Implement a synthetic feature", workspace
    )
    criterion = Criterion(
        id="c1", description="test", verification_argv=["python", "-m", "pytest", "-q", "tests/test_feature.py"]
    )
    step = PlanStep(id="s1", objective="implement feature", acceptance_criteria=[criterion])
    store.update_plan(task_id, [step.model_dump(mode="json")], 1)
    store.set_status(task_id, TaskStatus.PLANNING)
    model = FakeModel(
        [
            Delegate(
                action="delegate",
                reason_summary="first route",
                agent_id="codex",
                step_id="s1",
                objective=step.objective,
                acceptance_criteria=step.acceptance_criteria,
            ),
            Delegate(
                action="delegate",
                reason_summary="alternate route",
                agent_id="deepseek",
                step_id="s1",
                objective=step.objective,
                acceptance_criteria=step.acceptance_criteria,
            ),
        ]
    )
    runner = PreflightRunner()
    engine = TaskEngine(config, store, model_client=model, process_runner=runner)
    engine.adapters["codex_exec"] = PreflightAdapter(fail=True)
    engine.adapters["deepseek_headless"] = PreflightAdapter(fail=True)
    result = asyncio.run(engine.run_task(task_id))
    assert result.startswith("blocked:")
    assert store.runs(task_id) == []
    assert runner.calls == 0
    assert engine.adapters["codex_exec"].calls == 1
    assert engine.adapters["deepseek_headless"].calls == 1
    events = store.events(task_id)
    assert sum(event["type"] == "agent_call_reserved" for event in events) <= 2


def test_unavailable_execution_is_blocked_before_process_launch(
    config: AppConfiguration, store: Store, tmp_path: Path
) -> None:
    config = config.model_copy(
        update={
            "agents": {
                "codex": config.agents["codex"].model_copy(update={"allow_execution": False}),
                "deepseek": config.agents["deepseek"],
            }
        }
    )
    engine = TaskEngine(config, store, model_client=FakeModel([]))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_id, _ = engine.create_task("test", workspace)
    from agent_center.domain import Criterion
    from agent_center.engine import EngineBlocked

    step = PlanStep(
        id="s1",
        objective="do nothing",
        acceptance_criteria=[
            Criterion(
                id="c1", description="safe", verification_argv=["python", "-m", "pytest", "-q", "tests/test_safe.py"]
            )
        ],
    )
    store.update_plan(task_id, [step.model_dump(mode="json")], 1)
    with pytest.raises(EngineBlocked, match="execution is disabled"):
        asyncio.run(
            engine._delegate(
                task_id,
                Delegate(
                    action="delegate",
                    reason_summary="test",
                    agent_id="codex",
                    step_id="s1",
                    objective="do nothing",
                    acceptance_criteria=step.acceptance_criteria,
                ),
                1,
                "context",
            )
        )
    assert store.runs(task_id) == []


def test_single_task_guard_does_not_create_orphan_workspaces(config: AppConfiguration, store: Store) -> None:
    engine = TaskEngine(config, store)
    task_id, workspace = engine.create_task("First")
    before = set(workspace.parent.iterdir())
    with pytest.raises(RuntimeError, match=task_id):
        engine.create_task("Second")
    assert set(workspace.parent.iterdir()) == before
    assert len(store.list_tasks()) == 1

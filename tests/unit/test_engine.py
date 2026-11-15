from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agent_center.domain import DirectAnswer, TaskStatus
from agent_center.engine import TaskEngine
from agent_center.storage.database import Store


class FakeModel:
    def __init__(self, actions: list[Any]):
        self.actions = actions

    async def decide(self, **_: Any) -> Any:
        return self.actions.pop(0)

    async def answer_question(self, **_: Any) -> str:
        return "read only"


def test_direct_answer_completes_without_agent_calls(config: Any, store: Store, tmp_path: Path) -> None:
    model = FakeModel([DirectAnswer(action="answer", reason_summary="Simple question", answer="42")])
    engine = TaskEngine(config, store, model_client=model)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_id, _ = engine.create_task("What is 6x7?", workspace)
    assert asyncio.run(engine.run_task(task_id)) == "42"
    task = store.task(task_id)
    assert task["status"] == TaskStatus.SUCCEEDED
    assert task["agent_calls"] == 0


def test_external_publication_is_blocked_before_process_launch(config: Any, store: Store, tmp_path: Path) -> None:
    from agent_center.domain import Criterion, Delegate, PlanStep
    from agent_center.engine import EngineBlocked

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_id, _ = TaskEngine(config, store, model_client=FakeModel([])).create_task("push the result", workspace)
    step = PlanStep(
        id="publish",
        objective="push the result",
        acceptance_criteria=[
            Criterion(
                id="publish-proof",
                description="not supported",
                verification_argv=["python", "-m", "pytest", "-q", "tests/test_publish.py"],
            )
        ],
    )
    store.update_plan(task_id, [step.model_dump(mode="json")], 1)
    engine = TaskEngine(config, store, model_client=FakeModel([]))
    action = Delegate(
        action="delegate",
        reason_summary="test blocked publication",
        agent_id="codex",
        step_id="publish",
        objective=step.objective,
        acceptance_criteria=step.acceptance_criteria,
    )
    with pytest.raises(EngineBlocked, match="External"):
        asyncio.run(engine._delegate(task_id, action, 1, "context"))
    assert store.runs(task_id) == []


def test_publication_is_blocked_before_process_launch(config: Any, store: Store, tmp_path: Path) -> None:
    from agent_center.domain import Criterion, Delegate, PlanStep
    from agent_center.engine import EngineBlocked

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_id, _ = TaskEngine(config, store, model_client=FakeModel([])).create_task(
        "Push the deliverable to a remote registry", workspace
    )
    step = PlanStep(
        id="publish",
        objective="Push the deliverable to a remote registry",
        acceptance_criteria=[
            Criterion(
                id="public-check",
                description="public site is updated",
                verification_argv=["python", "-m", "pytest", "-q", "tests/test_public.py"],
            )
        ],
    )
    store.update_plan(task_id, [step.model_dump(mode="json")], 1)
    engine = TaskEngine(config, store, model_client=FakeModel([]))
    action = Delegate(
        action="delegate",
        reason_summary="test policy gate",
        agent_id="codex",
        step_id=step.id,
        objective=step.objective,
        acceptance_criteria=step.acceptance_criteria,
    )
    with pytest.raises(EngineBlocked, match="External"):
        asyncio.run(engine._delegate(task_id, action, 1, "context"))
    assert store.runs(task_id) == []


def test_unavailable_execution_is_blocked_before_process_launch(config: Any, store: Store, tmp_path: Path) -> None:
    # No credential, CLI, or provider request is needed to enforce the execution gate.
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
    from agent_center.domain import Criterion, Delegate, PlanStep
    from agent_center.engine import EngineBlocked

    step = PlanStep(
        id="s1",
        objective="do nothing",
        acceptance_criteria=[
            Criterion(
                id="c1",
                description="safe",
                verification_argv=["python", "-m", "pytest", "-q", "tests/test_safe.py"],
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

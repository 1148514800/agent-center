from __future__ import annotations

import asyncio
import json
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
        self.contexts: list[dict[str, Any]] = []

    async def decide(self, **kwargs: Any) -> Any:
        self.contexts.append(json.loads(kwargs["user"]))
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


@pytest.mark.parametrize("historical", [False, True])
def test_greeting_finishes_in_one_decision_preserving_history(
    config: AppConfiguration, store: Store, tmp_path: Path, historical: bool
) -> None:
    model = FakeModel([DirectAnswer(action="answer", reason_summary="Greeting", answer="你好！")])
    engine = TaskEngine(config, store, model_client=model)
    task_id, _ = engine.create_task("你好", tmp_path)
    old_evidence = []
    if historical:
        store._connection.execute(
            "UPDATE tasks SET supervisor_calls=5,active_seconds=12,status='blocked',"
            "last_error='Multiple tool calls in one response are not supported' WHERE task_id=?", (task_id,)
        )
        store.add_evidence(task_id, 1, "supervisor", "Preserved prior evidence", complete=False)
        old_evidence = store.evidence(task_id)
    assert asyncio.run(engine.run_task(task_id)) == "你好！"
    task = store.task(task_id)
    assert task["status"] == TaskStatus.SUCCEEDED
    assert task["supervisor_calls"] == (6 if historical else 1)
    assert task["agent_calls"] == 0
    assert task["active_seconds"] >= (12 if historical else 0)
    assert "execution_required" not in task
    assert not store.runs(task_id)
    assert len(model.contexts) == 1
    assert sum(e["type"] == "supervisor_call_reserved" for e in store.events(task_id)) == 1
    assert all(row in store.evidence(task_id) for row in old_evidence)
    assert store.evidence(task_id)[-1]["summary"] == "你好！"


@pytest.mark.parametrize("history", ["plan", "past_plan", "agent_calls", "completed_run", "reconciled_run"])
def test_saved_execution_work_requires_verification_with_one_feedback_retry(
    config: AppConfiguration, store: Store, tmp_path: Path, history: str
) -> None:
    answer = DirectAnswer(action="answer", reason_summary="Claiming completion", answer="All done")
    model = FakeModel([answer, answer, answer])
    engine = TaskEngine(config, store, model_client=model)
    task_id, _ = engine.create_task("你好", tmp_path)
    if history == "agent_calls":
        store.record_call(task_id, "agent", 12)
    if history in {"plan", "past_plan", "completed_run", "reconciled_run"}:
        store.update_plan(task_id, [{"id": "s", "objective": "synthetic", "acceptance_criteria": [
            {"id": "c", "description": "synthetic", "verification_argv": []}
        ]}], 1)
        if history in {"completed_run", "reconciled_run"}:
            run_id = store.create_run(task_id, "codex", "synthetic", {}, tmp_path / "stdout.log", {}, 1)
            store.mark_run(run_id, "completed" if history == "completed_run" else "reconciled")
            # Isolate the run guard from the separate plan-history guard.
            store._connection.execute("UPDATE tasks SET plan_json='[]',criteria_json='[]' WHERE task_id=?", (task_id,))
            store._connection.execute("DELETE FROM events WHERE task_id=? AND type='plan_updated'", (task_id,))
        elif history == "past_plan":
            store.add_message(task_id, "谢谢")  # Applying this clears the current plan.
    result = asyncio.run(engine.run_task(task_id))
    assert "Direct answer cannot complete" in result
    task = store.task(task_id)
    assert task["status"] == TaskStatus.BLOCKED
    assert task["supervisor_calls"] == 2
    assert len(model.contexts) == 2 and len(model.actions) == 1
    assert "Direct answer cannot complete" in model.contexts[1]["additional"]
    assert not store.evidence(task_id)
    # The storage completion gate must agree with the engine even with supervisor evidence.
    evidence = store.add_evidence(task_id, task["requirements_version"], "supervisor", "fake success")
    store.set_status(task_id, TaskStatus.VERIFYING)
    assert not store.finish_if_current(task_id, task["requirements_version"], [evidence])


@pytest.mark.parametrize("repeat", [False, True])
def test_multi_action_protocol_retries_without_applying_any_batch_action(
    config: AppConfiguration, store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repeat: bool
) -> None:
    import httpx

    from agent_center.supervisor import TOOL_DEFINITIONS, ModelClient

    monkeypatch.setenv("AGENT_CENTER_BASE_URL", "https://synthetic.invalid/v1")
    monkeypatch.setenv("AGENT_CENTER_API_KEY", "SYNTHETIC_PROTOCOL_TEST_VALUE")
    names = [tool["function"]["name"] for tool in TOOL_DEFINITIONS]
    requests = []
    task_id = store.create_task("你好", tmp_path, {})

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 2:
            assert store.task(task_id)["status"] == TaskStatus.PLANNING
            assert not store.runs(task_id) and not store.evidence(task_id)
            feedback = json.loads(requests[-1]["messages"][-1]["content"])["additional"]
            assert all(name in feedback for name in names)
            assert "exactly one tool call or a direct answer" in feedback
            if not repeat:
                return httpx.Response(200, json={"choices": [{"message": {"content": "你好！"}}]})
        # Arguments are deliberately invalid and contain a canary: none may be applied or logged.
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [
            {"function": {"name": name, "arguments": "PRIVATE_ARGUMENT_CANARY"}} for name in names
        ]}}]})

    model = ModelClient(config, transport=httpx.MockTransport(respond))
    result = asyncio.run(TaskEngine(config, store, model_client=model).run_task(task_id))
    task = store.task(task_id)
    assert len(requests) == task["supervisor_calls"] == 2
    assert task["agent_calls"] == 0
    assert task["status"] == (TaskStatus.BLOCKED if repeat else TaskStatus.SUCCEEDED)
    if repeat:
        assert all(name in task["last_error"] for name in names)
        assert "Multiple tool calls" in result
    events = store.events(task_id)
    errors = [json.loads(e["payload"]) for e in events if e["type"] == "supervisor_protocol_error"]
    assert len(errors) == (2 if repeat else 1)
    assert errors[0]["tool_names"] == names and errors[0]["count"] == len(names)
    assert errors[0]["feedback_retry"] is True
    if repeat:
        assert errors[1]["feedback_retry"] is False
    assert "PRIVATE_ARGUMENT_CANARY" not in json.dumps(events)
    assert not store.runs(task_id) and not json.loads(task["plan_json"])
    assert not store._connection.execute("SELECT 1 FROM approvals WHERE task_id=?", (task_id,)).fetchone()

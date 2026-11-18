from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from agent_center.config import AppConfiguration, SupervisorConfig, load_config, offline_default_configuration
from agent_center.domain import TaskStatus
from agent_center.engine import TaskEngine
from agent_center.setup import apply_registrations, register_agent, supervisor_settings_path
from agent_center.storage.database import Store
from agent_center.supervisor import ModelClient
from agent_center.web_server import WebApplication


@pytest.fixture
def endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CENTER_BASE_URL", "https://synthetic.invalid/v1")
    monkeypatch.setenv("AGENT_CENTER_API_KEY", "SYNTHETIC_TEST_CREDENTIAL")


def fake_app(config: AppConfiguration, store: Store, responses: list[dict[str, Any]]) -> tuple[
    WebApplication, list[dict[str, Any]]
]:
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": responses.pop(0)}]})

    engine = TaskEngine(config, store, model_client=ModelClient(config, transport=httpx.MockTransport(respond)))
    engine.runner.run = AsyncMock(side_effect=AssertionError("Unexpected CLI"))
    return WebApplication(engine, store), requests


def execution() -> dict[str, Any]:
    return {"tool_calls": [{"function": {"name": "request_execution", "arguments": json.dumps(
        {"reason_summary": "Use a local CLI"})}}]}


def saved_run(store: Store, task_id: str, tmp_path: Path) -> str:
    store.update_plan(task_id, [{"id": "s", "objective": "Fake", "acceptance_criteria": [
        {"id": "c", "description": "Fake", "verification_argv": []}]}], 1)
    return store.create_run(task_id, "codex", "Fake", {}, tmp_path / "log", {}, 1)


@pytest.mark.parametrize("text", [
    "你好", "Why?", "Explain this idea", "修改代码是什么意思？", "🌀", "Implement in theory",
])
async def test_one_semantic_answer_creates_no_execution_objects(
    config: AppConfiguration, store: Store, endpoint: None, text: str,
) -> None:
    app, requests = fake_app(config, store, [{"content": "Direct answer"}])
    result = await app.api("POST", "/api/messages", {"text": text})
    assert result["messages"][-1]["answer"] == "Direct answer"
    assert len(requests) == 1
    assert not store.list_tasks()
    assert store._connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    assert not Path(config.app.default_workspace_root).exists()
    assert [t["function"]["name"] for t in requests[0]["tools"]] == ["request_execution"]
    app.engine.runner.run.assert_not_awaited()


async def test_semantic_execution_enters_saved_plan_and_keeps_safety_gates(
    config: AppConfiguration, store: Store, endpoint: None,
) -> None:
    criterion = {"id": "c", "description": "Test", "verification_argv": ["python", "-m", "pytest", "tests/test_x.py"]}
    plan = {"tool_calls": [{"function": {"name": "update_plan", "arguments": json.dumps({
        "reason_summary": "Plan", "steps": [{"id": "s", "objective": "Implement", "acceptance_criteria": [criterion]}]
    })}}]}
    delegate = {"tool_calls": [{"function": {"name": "delegate", "arguments": json.dumps({
        "agent_id": "codex", "step_id": "s", "objective": "Implement", "acceptance_criteria": [criterion],
        "reason_summary": "Execute"})}}]}
    app, requests = fake_app(config, store, [execution(), plan, delegate, delegate, delegate, delegate])
    result = await app.api("POST", "/api/messages", {"text": "Make it so"})
    task_id = result["task_id"]
    assert Path(store.task(task_id)["workspace"]).is_dir()
    await app.jobs[task_id]
    assert len(store.list_tasks()) == 1
    assert json.loads(store.task(task_id)["plan_json"])[0]["id"] == "s"
    assert store.task(task_id)["status"] == "blocked"
    assert not store.runs(task_id)  # Unverified capabilities still prohibit execution.
    assert len(requests) > 1
    app.engine.runner.run.assert_not_awaited()


async def test_chat_followups_isolated_from_old_chat_and_active_task(
    config: AppConfiguration, store: Store, tmp_path: Path, endpoint: None,
) -> None:
    app, requests = fake_app(config, store, [{"content": "A"}, {"content": "B"}, {"content": "C"}])
    task_id, _ = app.engine.create_task("PRIVATE_TASK_CONTEXT", tmp_path)
    first = await app.api("POST", "/api/messages", {"text": "CHAT_ONE"})
    await app.api("POST", "/api/messages", {"text": "FOLLOWUP", "conversation_id": first["conversation_id"]})
    await app.api("POST", "/api/messages", {"text": "CHAT_TWO"})
    second_context = requests[1]["messages"][1]["content"]
    assert "CHAT_ONE" in second_context and '"assistant": "A"' in second_context
    assert "CHAT_ONE" not in requests[2]["messages"][1]["content"]
    assert all("PRIVATE_TASK_CONTEXT" not in json.dumps(r) for r in requests)
    assert len(store.messages(task_id)) == 1
    await app.api("POST", f"/api/tasks/{task_id}/message", {"text": "EXPLICIT_APPEND"})
    assert store.pending_messages(task_id) == ["EXPLICIT_APPEND"]


@pytest.mark.parametrize("unreconciled", [False, True])
async def test_blocked_execution_request_is_retained_and_retry_uses_saved_decision(
    config: AppConfiguration, store: Store, tmp_path: Path, endpoint: None, unreconciled: bool,
) -> None:
    app, requests = fake_app(config, store, [execution(), {"content": "Planning stopped"}])
    old_id, _ = app.engine.create_task("Old", tmp_path)
    if unreconciled:
        run_id = saved_run(store, old_id, tmp_path)
        store.mark_run(run_id, "unknown")
        store.set_status(old_id, TaskStatus.CANCELLED)
    result = await app.api("POST", "/api/messages", {"text": "NEW_REQUEST"})
    message = result["messages"][-1]
    assert message["status"] == "execution_blocked" and old_id in message["reason"]
    assert len(store.list_tasks()) == 1
    assert not Path(config.app.default_workspace_root).exists()
    assert len(store.messages(old_id)) == 1
    if unreconciled:
        store.reconcile_task_runs(old_id, user_confirmed=True)
    else:
        store.set_status(old_id, TaskStatus.CANCELLED)
    retried = await app.api("POST", f"/api/conversations/{result['conversation_id']}/execute",
                            {"message_id": message["message_id"]})
    assert retried["task_id"] != old_id
    assert len(requests) == 1
    await app.jobs[retried["task_id"]]


async def test_routing_failure_retains_message_without_defaulting_to_execution(
    config: AppConfiguration, store: Store, endpoint: None,
) -> None:
    app, _ = fake_app(config, store, [{"tool_calls": [{"function": {"name": "delegate", "arguments": "{}"}}]}])
    result = await app.api("POST", "/api/messages", {"text": "Unmatched wording"})
    assert result["messages"][-1]["status"] == "error"
    assert not store.list_tasks() and not Path(config.app.default_workspace_root).exists()


async def test_delete_cleans_internal_rows_and_keeps_workspace_and_chat(
    config: AppConfiguration, store: Store, tmp_path: Path,
) -> None:
    engine = TaskEngine(config, store)
    task_id, _ = engine.create_task("Task", tmp_path)
    sentinel = tmp_path / "user-file.txt"
    sentinel.write_text("keep")
    run_id = saved_run(store, task_id, tmp_path)
    store.mark_run(run_id, "completed", event_complete=True)
    evidence_id = store.add_evidence(task_id, 1, "supervisor", "Fake", run_id=run_id)
    store.link_evidence_criteria(evidence_id, ["c"])
    store._connection.execute(
        "INSERT INTO approvals(approval_id,task_id,requirements_version,config_hash,operation,target_path,"
        "risk,status,created_at,expires_at) VALUES('a',?,1,'h','x','x','x','denied','now','0')", (task_id,))
    chat_id = store.create_conversation()
    message_id = store.add_chat_message(chat_id, "Task", "")
    store.update_chat_message(message_id, status="task_created", task_id=task_id)
    store.set_status(task_id, TaskStatus.CANCELLED)
    store.settle_active_period(task_id)
    app = WebApplication(engine, store)
    with pytest.raises(ValueError, match="确认删除"):
        await app.api("POST", f"/api/tasks/{task_id}/delete", {})
    assert await app.api("POST", f"/api/tasks/{task_id}/delete", {"confirmation": task_id}) == {"deleted": task_id}
    assert not store.list_tasks()
    for table in ("events", "runs", "approvals", "evidence", "evidence_criteria", "messages"):
        assert store._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert sentinel.read_text() == "keep"
    assert store.conversation(chat_id)["messages"][0]["task_id"] is None
    with pytest.raises(ValueError, match="不存在"):
        await app.api("POST", f"/api/tasks/{task_id}/delete", {"confirmation": task_id})


@pytest.mark.parametrize("status", ["prepared", "starting", "running", "unknown", "failed", "cancelled", "timed_out"])
def test_delete_refuses_unreconciled_run_even_in_terminal_task(
    config: AppConfiguration, store: Store, tmp_path: Path, status: str,
) -> None:
    task_id, _ = TaskEngine(config, store).create_task("Task", tmp_path)
    run_id = saved_run(store, task_id, tmp_path)
    store.mark_run(run_id, status)
    store.set_status(task_id, TaskStatus.CANCELLED)
    with pytest.raises(ValueError, match="对账"):
        store.delete_task(task_id)
    assert store.task(task_id) and store.runs(task_id)


async def test_delete_refuses_live_web_job(config: AppConfiguration, store: Store, tmp_path: Path) -> None:
    app = WebApplication(TaskEngine(config, store), store)
    task_id, _ = app.engine.create_task("Task", tmp_path)
    job = asyncio.create_task(asyncio.Event().wait())
    app.jobs[task_id] = job
    try:
        with pytest.raises(ValueError, match="停止"):
            await app.api("POST", f"/api/tasks/{task_id}/delete", {"confirmation": task_id})
    finally:
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)


@pytest.mark.parametrize(("model", "effort"), [("gpt-5", "high"), ("o3", "low"), ("gpt-5.2", "xhigh"),
                                                 ("unknown-provider-model", ""), ("gpt-5", "")])
async def test_protocol_reasoning_payload(
    config: AppConfiguration, store: Store, endpoint: None, model: str, effort: str,
) -> None:
    config = config.model_copy(update={"supervisor": SupervisorConfig(
        protocol="openai_chat_completions", model=model, reasoning_effort=effort)})
    app, requests = fake_app(config, store, [{"content": "A"}])
    await app.api("POST", "/api/messages", {"text": "Q"})
    assert requests[0].get("reasoning_effort", "") == effort
    if not effort:
        assert "reasoning_effort" not in requests[0]


@pytest.mark.parametrize(("protocol", "model", "effort"), [("openai_chat_completions", "custom", "high"),
    ("openai_chat_completions", "gpt-5", "ultra"), ("anthropic", "gpt-5", "high")])
def test_unsupported_reasoning_or_protocol_rejected(protocol: str, model: str, effort: str) -> None:
    with pytest.raises(ValueError):
        SupervisorConfig.model_validate({"protocol": protocol, "model": model,
                                         "reasoning_effort": effort}).validate_protocol_options()


async def test_durable_settings_reload_and_never_serialize_secrets(
    config: AppConfiguration, store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "custom" / "app.toml"
    path.parent.mkdir()
    path.write_bytes(Path("config.example.toml").read_bytes())
    original = path.read_bytes()
    app = WebApplication(TaskEngine(config, store), store, path)
    values = {"protocol": "openai_chat_completions", "model": "gpt-5", "reasoning_effort": "high",
              "base_url": "https://synthetic.invalid/v1", "api_key": "SYNTHETIC_PRIVATE_CANARY"}
    await app.api("POST", "/api/supervisor", values)
    monkeypatch.delenv("AGENT_CENTER_BASE_URL", raising=False)
    monkeypatch.delenv("AGENT_CENTER_API_KEY", raising=False)
    restored = load_config(path)
    restarted = WebApplication(TaskEngine(restored, store), store, path)
    state = await restarted.api("GET", "/api/state", {})
    assert state["supervisor_settings"]["reasoning_effort"] == "high"
    assert state["supervisor_settings"]["key_configured"]
    assert restored.supervisor_endpoint() == (values["base_url"], values["api_key"], "gpt-5")
    assert values["api_key"] not in json.dumps(state)
    assert values["api_key"] not in restored.model_dump_json()
    assert path.read_bytes() == original
    assert supervisor_settings_path(path).stat().st_mode & 0o777 == 0o600
    await restarted.api("POST", "/api/supervisor", {**values, "api_key": "", "base_url": ""})
    assert load_config(path).supervisor_endpoint()[1] == values["api_key"]
    assert not (tmp_path / "app.toml.supervisor.json").exists()
    assert "localStorage" not in Path("src/agent_center/web/app.js").read_text()


async def test_discovered_tools_are_hidden_until_explicit_registration(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "fake-codex"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    monkeypatch.setattr("agent_center.setup.discover_agents", lambda: [
        {"agent_id": "codex", "executable": str(executable), "cli_install_dir": ""}])
    config = offline_default_configuration()
    app = WebApplication(TaskEngine(config, store), store)
    state = await app.api("GET", "/api/state", {})
    assert state["agents"] == [] and state["supervisor_settings"]
    assert await app.api("GET", "/api/agents/health", {}) == {}
    config = register_agent(config, tmp_path / "config.toml", {"agent_id": "codex", "model": ""})
    restored = apply_registrations(offline_default_configuration(), tmp_path / "config.toml")
    app.update_configuration(restored)
    assert [a["id"] for a in (await app.api("GET", "/api/state", {}))["agents"]] == ["codex"]
    assert list(await app.api("GET", "/api/agents/health", {})) == ["codex"]


async def test_unsupported_effort_fails_before_transport_and_keeps_prior_settings(
    config: AppConfiguration, store: Store, tmp_path: Path,
) -> None:
    path = tmp_path / "selected.toml"
    app = WebApplication(TaskEngine(config, store), store, path)
    await app.api("POST", "/api/supervisor", {"protocol": "openai_chat_completions", "model": "custom-model",
        "base_url": "https://synthetic.invalid/v1", "api_key": "SYNTHETIC_DURABLE_VALUE"})
    original = supervisor_settings_path(path).read_bytes()
    with pytest.raises(ValueError, match="模型默认"):
        await app.api("POST", "/api/supervisor", {"protocol": "openai_chat_completions", "model": "custom-model",
                                               "reasoning_effort": "high"})
    assert supervisor_settings_path(path).read_bytes() == original
    old = config.model_copy(update={"supervisor": SupervisorConfig(
        protocol="openai_chat_completions", model="custom-model", reasoning_effort="high")})
    model = ModelClient(old, transport=httpx.MockTransport(lambda _: pytest.fail("No HTTP allowed")))
    with pytest.raises(ValueError, match="模型默认"):
        await model.route(user="Q")


async def test_restart_retains_conversation_and_saved_execution_request(
    config: AppConfiguration, store: Store, tmp_path: Path, endpoint: None,
) -> None:
    app, _ = fake_app(config, store, [{"content": "Previous answer"}, execution()])
    task_id, _ = app.engine.create_task("Existing", tmp_path)
    first = await app.api("POST", "/api/messages", {"text": "Question"})
    result = await app.api("POST", "/api/messages", {"text": "Execute followup",
                                                    "conversation_id": first["conversation_id"]})
    with_store = Store(store.data_dir)
    try:
        restored = with_store.conversation(result["conversation_id"])
        assert restored["messages"][0]["answer"] == "Previous answer"
        assert restored["messages"][-1]["status"] == "execution_blocked"
        assert task_id in restored["messages"][-1]["reason"]
    finally:
        with_store.close()


async def test_empty_connections_legacy_config_and_custom_data_dir_remain_compatible(
    config: AppConfiguration, store: Store, tmp_path: Path,
) -> None:
    # Existing concrete TOML agents do not need the new user_added field.
    app = WebApplication(TaskEngine(config, store), store, tmp_path / "custom.toml")
    assert {a["id"] for a in (await app.api("GET", "/api/state", {}))["agents"]} == {"codex", "deepseek"}
    await app.api("POST", "/api/supervisor", {"protocol": "openai_chat_completions", "model": "custom-model",
        "base_url": "https://synthetic.invalid/v1", "api_key": "SYNTHETIC_LOCAL_VALUE"})
    restored = apply_registrations(config, tmp_path / "custom.toml")
    changed = restored.model_copy(update={"app": restored.app.model_copy(update={
        "data_dir": str(tmp_path / "other-data")})})
    assert changed.supervisor_endpoint()[1] == "SYNTHETIC_LOCAL_VALUE"
    assert not (tmp_path / "other-data").exists()


async def test_secret_file_permissions_and_atomic_failure(
    config: AppConfiguration, store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "private" / "custom.toml"
    app = WebApplication(TaskEngine(config, store), store, path)
    values = {"protocol": "openai_chat_completions", "model": "custom-model",
              "base_url": "https://synthetic.invalid/v1", "api_key": "SYNTHETIC_ATOMIC_VALUE"}
    await app.api("POST", "/api/supervisor", values)
    secret = supervisor_settings_path(path)
    assert secret.parent.stat().st_mode & 0o777 == 0o700
    original = secret.read_bytes()

    def fail_replace(*args: Any) -> None:
        raise OSError("simulated atomic write failure")

    monkeypatch.setattr("agent_center.setup.os.replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        await app.api("POST", "/api/supervisor", {**values, "model": "changed"})
    assert secret.read_bytes() == original
    assert sorted(p.name for p in secret.parent.iterdir()) == [secret.name]
    secret.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        apply_registrations(config, path)


@pytest.mark.parametrize("response", [{"tool_calls": ["malformed"]}, {"tool_calls": [{"function": []}]},
                                      {"tool_calls": [execution()["tool_calls"][0]] * 2}])
async def test_malformed_routing_never_falls_back_to_execution(
    config: AppConfiguration, store: Store, endpoint: None, response: dict[str, Any],
) -> None:
    app, requests = fake_app(config, store, [response])
    result = await app.api("POST", "/api/messages", {"text": "Arbitrary request"})
    assert result["messages"][-1]["status"] == "error"
    assert len(requests) == 1 and not store.list_tasks()
    assert not Path(config.app.default_workspace_root).exists()

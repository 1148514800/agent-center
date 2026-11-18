from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_center.config import AppConfiguration
from agent_center.domain import TaskStatus
from agent_center.engine import TaskEngine
from agent_center.storage.database import Store
from agent_center.web_server import WebApplication, serve


@pytest.mark.asyncio
async def test_web_task_creation_preserves_configuration_gates(
    config: AppConfiguration, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = WebApplication(TaskEngine(config, store), store)
    from unittest.mock import AsyncMock

    from agent_center.domain import ExecutionRequest

    monkeypatch.setattr(app.engine.model, "route", AsyncMock(return_value=ExecutionRequest(
        action="request_execution", reason_summary="Needs CLI")))
    result = await app.api("POST", "/api/tasks", {"goal": "Implement a feature"})
    task_id = result["task_id"]
    result = app.detail(task_id)
    assert result["task"]["status"] == "blocked"
    assert result["task"]["last_error"]
    assert not store.runs(task_id)
    await app.api("POST", f"/api/tasks/{task_id}/message", {"text": "Add tests"})
    assert store.pending_messages(task_id)
    await app.api("POST", f"/api/tasks/{task_id}/cancel", {})
    assert store.task(task_id)["status"] == "cancelled"


@pytest.mark.asyncio
async def test_web_rejects_resume_with_unresolved_run(config: AppConfiguration, store: Store, tmp_path: Path) -> None:
    engine = TaskEngine(config, store)
    task_id, _ = engine.create_task("Implement", tmp_path)
    prepare_run(engine, task_id, tmp_path)
    store._connection.execute("UPDATE tasks SET status='interrupted' WHERE task_id=?", (task_id,))
    app = WebApplication(engine, store)
    with pytest.raises(ValueError):
        await app.api(
            "POST",
            f"/api/tasks/{task_id}/approval",
            {
                "approval_id": "forged",
                "confirmation": "forged",
                "decision": "approve",
            },
        )
    with pytest.raises(ValueError):
        await app.api("POST", f"/api/tasks/{task_id}/resume", {})
    assert store.runs(task_id)[0]["status"] == "unknown"


@pytest.mark.asyncio
async def test_http_origin_host_and_csrf_guards(
    config: AppConfiguration, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import re
    from unittest.mock import AsyncMock, MagicMock, Mock

    # Exercise the real HTTP handler with in-memory streams, including in socket-restricted sandboxes.
    port = 18765
    started = asyncio.Event()
    server = MagicMock()
    server.__aenter__.return_value = server

    async def serve_forever() -> None:
        started.set()
        await asyncio.Future()

    server.serve_forever = AsyncMock(side_effect=serve_forever)
    start_server = AsyncMock(return_value=server)
    monkeypatch.setattr(asyncio, "start_server", start_server)
    running = asyncio.create_task(serve(TaskEngine(config, store), store, port, open_browser=False))
    await asyncio.wait_for(started.wait(), 2)
    start_server.assert_called_once()
    handler = start_server.call_args.args[0]
    assert start_server.call_args.args[1:] == ("127.0.0.1", port)

    async def request(headers: str, method: str = "GET", path: str = "/", body: bytes = b"") -> bytes:
        reader = asyncio.StreamReader()
        reader.feed_data(
            f"{method} {path} HTTP/1.1\r\n{headers}\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
        )
        reader.feed_eof()
        writer = Mock(drain=AsyncMock(), wait_closed=AsyncMock())
        await handler(reader, writer)
        writer.close.assert_called_once()
        return writer.write.call_args.args[0]

    try:
        response = await request(f"Host: 127.0.0.1:{port}")
        assert b"200 OK" in response and b"__CSRF_TOKEN__" not in response
        for splitter_id, orientation, controls in (
            ("sidebar-split", "vertical", "sidebar"),
            ("history-split", "horizontal", "tasks chats"),
            ("reading-split", "horizontal", "reading-pane prompt"),
        ):
            splitter = re.search(fr'<div id="{splitter_id}"[^>]*>'.encode(), response)
            assert splitter
            for attribute in (
                'role="separator"', 'tabindex="0"', 'aria-label="',
                f'aria-controls="{controls}"', f'aria-orientation="{orientation}"',
            ):
                assert attribute.encode() in splitter[0]
        assert b'id="reset-layout"' in response
        assert b"400 Bad Request" in await request("Host: attacker.example")
        assert b"400 Bad Request" in await request(f"Host: 127.0.0.1:{port}\r\nOrigin: https://attacker.example")
        assert b"400 Bad Request" in await request(f"Host: 127.0.0.1:{port}", "POST", "/api/tasks")
        token = re.search(rb'name="agent-center-token" content="([^"]+)"', response)
        assert token
        headers = f"Host: 127.0.0.1:{port}\r\nX-Agent-Center-Token: {token[1].decode()}"
        directory = await request(
            headers, "POST", "/api/directories", json.dumps({"path": str(store.data_dir)}).encode()
        )
        assert b"200 OK" in directory
        assert "entries" in json.loads(directory.split(b"\r\n\r\n", 1)[1])
        state = await request(f"Host: 127.0.0.1:{port}", path="/api/state")
        assert json.loads(state.split(b"\r\n\r\n", 1)[1])["api_revision"] == 4
        task_id = store.create_task("Synthetic task", store.data_dir, {})
        delete_path = f"/api/tasks/{task_id}/delete"
        body = json.dumps({"confirmation": task_id}).encode()
        assert b"400 Bad Request" in await request(f"Host: 127.0.0.1:{port}", "POST", delete_path, body)
        assert store.task(task_id)
        deleted = await request(headers, "POST", delete_path, body)
        assert b"200 OK" in deleted and json.loads(deleted.split(b"\r\n\r\n", 1)[1]) == {"deleted": task_id}
        assert not store.list_tasks()
    finally:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running


def prepare_run(engine: TaskEngine, task_id: str, tmp_path: Path, status: str = "unknown") -> str:
    engine.store.update_plan(task_id, [{"id": "s", "objective": "synthetic", "acceptance_criteria": [
        {"id": "c", "description": "synthetic", "verification_argv": []}
    ]}], 1)
    run_id = engine.store.create_run(task_id, "codex", "synthetic", {}, tmp_path / "stdout.log", {}, 1)
    engine.store.mark_run(run_id, status)
    return run_id


@pytest.mark.parametrize("run_status", ["running", "unknown", "failed"])
async def test_sidebar_delete_endpoint_refuses_protected_runs(
    config: AppConfiguration, store: Store, tmp_path: Path, run_status: str,
) -> None:
    app = WebApplication(TaskEngine(config, store), store)
    task_id, _ = app.engine.create_task("Synthetic task", tmp_path)
    run_id = prepare_run(app.engine, task_id, tmp_path, run_status)
    store.set_status(task_id, TaskStatus.CANCELLED)

    with pytest.raises(ValueError, match="停止 / 取消运行并对账"):
        await app.api("POST", f"/api/tasks/{task_id}/delete", {"confirmation": task_id})

    assert store.task(task_id)
    assert store.runs(task_id)[0]["run_id"] == run_id


@pytest.mark.asyncio
async def test_active_task_is_discoverable_and_accepts_followups_without_a_second_task(
    config: AppConfiguration, store: Store, tmp_path: Path
) -> None:
    engine = TaskEngine(config, store)
    task_id, _ = engine.create_task("First", tmp_path)
    app = WebApplication(engine, store)
    store.set_status(task_id, TaskStatus.BLOCKED)
    state = await app.api("GET", "/api/state", {})
    assert state["active_task_id"] == task_id
    result = await app.api("POST", f"/api/tasks/{task_id}/message", {"text": "Continue this task"})
    assert result["messages"][-1]["text"] == "Continue this task"
    from unittest.mock import AsyncMock

    from agent_center.domain import ExecutionRequest

    app.engine.model.route = AsyncMock(return_value=ExecutionRequest(
        action="request_execution", reason_summary="Needs CLI"))
    result = await app.api("POST", "/api/tasks", {"goal": "Second"})
    assert result["messages"][-1]["status"] == "execution_blocked"
    assert task_id in result["messages"][-1]["reason"]
    assert len(store.list_tasks()) == 1


@pytest.mark.asyncio
async def test_paused_followup_resumes_but_running_followup_does_not_duplicate_job(
    config: AppConfiguration, store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    from agent_center.supervisor import ModelClient

    monkeypatch.setenv("AGENT_CENTER_BASE_URL", "https://synthetic.invalid/v1")
    monkeypatch.setenv("AGENT_CENTER_API_KEY", "synthetic-test-value")
    entered, release = asyncio.Event(), asyncio.Event()

    async def respond(_: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"choices": [{"message": {"content": "42"}}]})

    engine = TaskEngine(config, store, model_client=ModelClient(config, transport=httpx.MockTransport(respond)))
    task_id, _ = engine.create_task("What is 6x7?", tmp_path)
    store.set_status(task_id, TaskStatus.PAUSED)
    app = WebApplication(engine, store)
    try:
        result = await app.api("POST", f"/api/tasks/{task_id}/message", {"text": "Please explain"})
        assert result["running"]
        await asyncio.wait_for(entered.wait(), 2)
        job = app.jobs[task_id]
        await app.api("POST", f"/api/tasks/{task_id}/message", {"text": "Use simple language"})
        assert app.jobs[task_id] is job
        assert len(store.list_tasks()) == 1
    finally:
        release.set()
        await app.jobs[task_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["blocked", "interrupted", "running", "verifying", "planning", "waiting_approval"])
async def test_stale_task_can_be_reviewed_then_cancelled_without_losing_history(
    config: AppConfiguration, store: Store, tmp_path: Path, status: str
) -> None:
    engine = TaskEngine(config, store)
    task_id, _ = engine.create_task("First", tmp_path)
    prepare_run(engine, task_id, tmp_path)
    store._connection.execute("UPDATE tasks SET status=? WHERE task_id=?", (status, task_id))
    app = WebApplication(engine, store)
    assert app.detail(task_id)["recovery"]["needs_review"]
    with pytest.raises(ValueError):
        await app.api("POST", f"/api/tasks/{task_id}/resume", {})
    with pytest.raises(ValueError):
        await app.api("POST", f"/api/tasks/{task_id}/reconcile", {"confirmation": "wrong", "reviewed": True})
    result = await app.api("POST", f"/api/tasks/{task_id}/reconcile", {"confirmation": task_id, "reviewed": True})
    assert result["task"]["status"] == "planning"
    assert result["recovery"]["can_resume"]
    assert not result["recovery"]["needs_review"]
    await app.api("POST", f"/api/tasks/{task_id}/cancel", {})
    assert store.messages(task_id)[0]["text"] == "First"
    assert any(e["type"] == "runs_reconciled" for e in store.events(task_id))
    assert engine.create_task("Second", tmp_path)[0] != task_id


@pytest.mark.asyncio
async def test_unknown_run_requires_review_and_is_never_replayed(
    config: AppConfiguration, store: Store, tmp_path: Path
) -> None:
    engine = TaskEngine(config, store)
    task_id, _ = engine.create_task("First", tmp_path)
    run_id = prepare_run(engine, task_id, tmp_path)
    app = WebApplication(engine, store)
    for action in ("resume", "cancel"):
        with pytest.raises(ValueError):
            await app.api("POST", f"/api/tasks/{task_id}/{action}", {})
    assert store.runs(task_id)[0]["status"] == "unknown"
    assert (await app.api("GET", "/api/state", {}))["unresolved_task_ids"] == [task_id]
    result = await app.api("POST", f"/api/tasks/{task_id}/reconcile", {"confirmation": task_id, "reviewed": True})
    assert result["runs"][0]["run_id"] == run_id
    assert result["runs"][0]["status"] == "reconciled"
    assert not result["running"] and not result["plan"]
    assert len(store.runs(task_id)) == 1
    assert result["recovery"]["can_resume"]
    await app.api("POST", f"/api/tasks/{task_id}/cancel", {})
    assert store.task(task_id)["status"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["blocked", "interrupted", "planning", "running", "verifying"])
@pytest.mark.parametrize("action", ["resume", "cancel"])
async def test_no_run_failure_can_resume_or_cancel_without_reconciliation(
    config: AppConfiguration, store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    status: str, action: str,
) -> None:
    import httpx

    from agent_center.supervisor import ModelClient

    monkeypatch.setenv("AGENT_CENTER_BASE_URL", "https://synthetic.invalid/v1")
    monkeypatch.setenv("AGENT_CENTER_API_KEY", "SYNTHETIC_RECOVERY_TEST_VALUE")
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "你好！"}}]})

    engine = TaskEngine(config, store, model_client=ModelClient(config, transport=httpx.MockTransport(respond)))
    task_id, _ = engine.create_task("你好", tmp_path)
    error = "Multiple tool calls (2): inspect, delegate"
    store._connection.execute(
        "UPDATE tasks SET status=?,last_error=?,supervisor_calls=6 WHERE task_id=?",
        (status, error, task_id),
    )
    app = WebApplication(engine, store)
    recovery = (await app.api("GET", f"/api/tasks/{task_id}", {}))["recovery"]
    assert recovery["can_resume"] and not recovery["needs_review"]
    assert not recovery["can_reconcile"]
    assert error in recovery["hint"]
    assert all(word not in recovery["hint"] for word in ["Run", "日志", "工作区", "对账"])
    await app.api("POST", f"/api/tasks/{task_id}/{action}", {})
    if action == "resume":
        await app.jobs[task_id]
    assert store.task(task_id)["status"] == ("succeeded" if action == "resume" else "cancelled")
    assert len(requests) == (1 if action == "resume" else 0)
    assert store.task(task_id)["supervisor_calls"] == (7 if action == "resume" else 6)
    assert not store.runs(task_id)
    assert not any(e["type"] == "runs_reconciled" for e in store.events(task_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("alive", [True, None])
async def test_web_refuses_reconciliation_of_live_or_ambiguous_process(
    config: AppConfiguration, store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alive: bool | None
) -> None:
    engine = TaskEngine(config, store)
    task_id, _ = engine.create_task("First", tmp_path)
    prepare_run(engine, task_id, tmp_path)
    monkeypatch.setattr("agent_center.web_server.verify_process_identity", lambda *args: alive)
    app = WebApplication(engine, store)
    with pytest.raises(ValueError, match="进程仍在运行或身份不确定"):
        await app.api("POST", f"/api/tasks/{task_id}/reconcile", {"confirmation": task_id, "reviewed": True})
    assert store.runs(task_id)[0]["status"] == "unknown"


@pytest.mark.asyncio
async def test_reconciling_terminal_task_does_not_resurrect_it(
    config: AppConfiguration, store: Store, tmp_path: Path
) -> None:

    engine = TaskEngine(config, store)
    task_id, _ = engine.create_task("First", tmp_path)
    prepare_run(engine, task_id, tmp_path)
    store.set_status(task_id, TaskStatus.CANCELLED)
    app = WebApplication(engine, store)
    result = await app.api("POST", f"/api/tasks/{task_id}/reconcile", {"confirmation": task_id, "reviewed": True})
    assert result["task"]["status"] == "cancelled"
    assert not result["recovery"]["can_resume"]
    assert not store.unresolved_runs()


@pytest.mark.asyncio
@pytest.mark.parametrize("approval_status", ["pending", "denied", "expired", "missing"])
async def test_resolved_or_stale_approval_pointer_does_not_trap_recovery(
    config: AppConfiguration, store: Store, tmp_path: Path, approval_status: str
) -> None:
    engine = TaskEngine(config, store)
    task_id, _ = engine.create_task("First", tmp_path)
    prepare_run(engine, task_id, tmp_path, "failed")
    store.set_status(task_id, TaskStatus.BLOCKED, pending_approval_id="approval-test",
                     pending_action={"action": "request_approval"})
    if approval_status != "missing":
        store._connection.execute(
            "INSERT INTO approvals(approval_id,task_id,requirements_version,config_hash,operation,"
            "target_path,risk,status,created_at,expires_at) VALUES (?,?,1,'hash','test',?,'test',?,'now','0')",
            ("approval-test", task_id, str(tmp_path), approval_status),
        )
    app = WebApplication(engine, store)
    if approval_status == "pending":
        assert not app.detail(task_id)["recovery"]["can_reconcile"]
        with pytest.raises(ValueError, match="审批"):
            await app.api("POST", f"/api/tasks/{task_id}/reconcile", {"confirmation": task_id, "reviewed": True})
        assert store.task(task_id)["pending_approval_id"]
    else:
        assert app.detail(task_id)["recovery"]["can_reconcile"]
        result = await app.api("POST", f"/api/tasks/{task_id}/reconcile",
                               {"confirmation": task_id, "reviewed": True})
        assert result["recovery"]["can_resume"]
        assert store.task(task_id)["pending_approval_id"] is None


@pytest.mark.asyncio
async def test_agent_health_distinguishes_missing_verified_and_stale_installations(
    verified_config: AppConfiguration, store: Store
) -> None:
    app = WebApplication(TaskEngine(verified_config, store), store)
    health = await app.api("GET", "/api/agents/health", {})
    assert health["codex"]["status"] == "已验证"
    executable = Path(verified_config.agents["codex"].executable)
    executable.write_text("#!/bin/sh\n# changed installation\nexit 0\n")
    assert (await app.api("GET", "/api/agents/health", {}))["codex"]["status"] == "未验证"
    executable.unlink()
    assert (await app.api("GET", "/api/agents/health", {}))["codex"]["status"] == "不可用"

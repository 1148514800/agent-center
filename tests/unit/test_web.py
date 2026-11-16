from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_center.config import AppConfiguration
from agent_center.engine import TaskEngine
from agent_center.storage.database import Store
from agent_center.web_server import WebApplication, serve


@pytest.mark.asyncio
async def test_web_task_creation_preserves_configuration_gates(config: AppConfiguration, store: Store) -> None:
    app = WebApplication(TaskEngine(config, store), store)
    result = await app.api("POST", "/api/tasks", {"goal": "Implement a feature"})
    task_id = result["task"]["task_id"]
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
    assert not store.runs(task_id)


@pytest.mark.asyncio
async def test_http_origin_host_and_csrf_guards(config: AppConfiguration, store: Store) -> None:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    running = asyncio.create_task(serve(TaskEngine(config, store), store, port, open_browser=False))

    async def request(headers: str, method: str = "GET", path: str = "/") -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"{method} {path} HTTP/1.1\r\n{headers}\r\n\r\n".encode())
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
        return response

    try:
        for _ in range(50):
            try:
                response = await request(f"Host: 127.0.0.1:{port}")
                break
            except ConnectionRefusedError:
                await asyncio.sleep(0.01)
        else:
            pytest.fail("Server did not start")
        assert b"200 OK" in response and b"__CSRF_TOKEN__" not in response
        assert b"400 Bad Request" in await request("Host: attacker.example")
        assert b"400 Bad Request" in await request(f"Host: 127.0.0.1:{port}\r\nOrigin: https://attacker.example")
        assert b"400 Bad Request" in await request(f"Host: 127.0.0.1:{port}", "POST", "/api/tasks")
        assert not store.list_tasks()
    finally:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

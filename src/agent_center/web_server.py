"""Loopback-only browser UI. All state and execution remain owned by TaskEngine."""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import webbrowser
from pathlib import Path
from typing import Any

from agent_center.domain import TaskStatus
from agent_center.engine import TaskEngine
from agent_center.storage.database import Store
from agent_center.storage.redaction import redact_text, redact_value


class WebApplication:
    def __init__(self, engine: TaskEngine, store: Store):
        self.engine = engine
        self.store = store
        self.token = secrets.token_urlsafe(32)
        self.jobs: dict[str, asyncio.Task[None]] = {}
        self.controls: dict[str, tuple[asyncio.Event, asyncio.Event]] = {}

    def detail(self, task_id: str) -> dict[str, Any]:
        task = self.store.task(task_id)
        if task is None:
            raise ValueError("任务不存在")
        return {
            "task": {
                k: task[k]
                for k in (
                    "task_id",
                    "goal",
                    "workspace",
                    "status",
                    "requirements_version",
                    "agent_calls",
                    "supervisor_calls",
                    "active_seconds",
                    "last_error",
                )
            },
            "plan": json.loads(task["plan_json"]),
            "messages": self.store.messages(task_id),
            "events": self.store.events(task_id)[-200:],
            "runs": self.store.runs(task_id),
            "evidence": self.store.evidence(task_id),
            "approvals": [
                dict(row)
                for row in self.store._connection.execute(
                    "SELECT approval_id,operation,target_path,risk,status FROM approvals WHERE task_id=?", (task_id,)
                )
            ],
            "running": task_id in self.jobs and not self.jobs[task_id].done(),
        }

    def start(self, task_id: str) -> None:
        if any(not job.done() for job in self.jobs.values()):
            raise ValueError("已有任务正在运行")
        if self.store.unresolved_runs():
            raise ValueError("存在未对账的 Run，请使用 CLI 检查进程与工作区")
        task = self.store.task(task_id)
        if not task or task["status"] not in {"created", "paused", "interrupted", "planning"}:
            raise ValueError("此任务不能直接继续，请检查阻塞原因")
        self.engine.config.supervisor_endpoint()
        cancel, pause = asyncio.Event(), asyncio.Event()
        self.controls[task_id] = cancel, pause

        async def work() -> None:
            try:
                answer = await self.engine.run_task(task_id, cancel_event=cancel, pause_event=pause)
                self.store.append_event(task_id, "web_response", {"text": redact_text(answer)})
            except asyncio.CancelledError:
                self.store.set_status(task_id, TaskStatus.INTERRUPTED, error="网页服务已停止，任务现场已保存")
                raise
            except Exception as exc:
                self.store.set_status(task_id, TaskStatus.BLOCKED, error=redact_text(str(exc)))

        self.jobs[task_id] = asyncio.create_task(work())

    async def api(self, method: str, path: str, body: dict[str, Any]) -> Any:
        if method == "GET" and path == "/api/state":
            config = self.engine.config
            return {
                "tasks": self.store.list_tasks(100),
                "agents": [
                    {
                        "id": name,
                        "enabled": a.enabled and a.allow_execution,
                        "model": config.profiles[a.default_profile].model,
                        "permission": config.profiles[a.default_profile].permission_mode,
                        "capabilities": a.capabilities.model_dump(mode="json"),
                    }
                    for name, a in config.agents.items()
                ],
                "limits": config.limits.model_dump(),
                "supervisor": config.supervisor.model,
            }
        if method == "POST" and path == "/api/tasks":
            goal = str(body.get("goal", "")).strip()
            if not goal or len(goal) > 16000:
                raise ValueError("请输入 1–16000 字的任务")
            workspace = str(body.get("workspace", "")).strip()
            task_id, _ = self.engine.create_task(goal, Path(workspace) if workspace else None)
            try:
                self.start(task_id)
            except ValueError as exc:
                self.store.set_status(task_id, TaskStatus.BLOCKED, error=redact_text(str(exc)))
            return self.detail(task_id)
        parts = path.strip("/").split("/")
        if len(parts) < 3 or parts[:2] != ["api", "tasks"]:
            raise ValueError("接口不存在")
        task_id = parts[2]
        detail = self.detail(task_id)
        if method == "GET" and len(parts) == 3:
            return detail
        if method != "POST" or len(parts) != 4:
            raise ValueError("请求不支持")
        action = parts[3]
        if action == "message":
            text = str(body.get("text", "")).strip()
            if not text or len(text) > 16000:
                raise ValueError("消息为空或过长")
            if detail["task"]["status"] in {"succeeded", "cancelled", "failed"}:
                raise ValueError("任务已结束，请新建任务")
            self.store.add_message(task_id, text)
        elif action == "resume":
            self.start(task_id)
        elif action in {"pause", "stop", "cancel"}:
            if detail["running"]:
                cancel, pause = self.controls[task_id]
                (pause if action == "pause" else cancel).set()
                if action == "cancel":
                    await self.jobs[task_id]
            if action == "cancel":
                if any(r["status"] in {"prepared", "starting", "running", "unknown"} for r in self.store.runs(task_id)):
                    raise ValueError("Run 尚未对账，不能取消")
                if detail["task"]["status"] in {"succeeded", "failed", "cancelled"}:
                    raise ValueError("任务已结束")
                self.store.set_status(task_id, TaskStatus.CANCELLED, error="用户通过网页取消")
        elif action == "approval":
            approval_id = str(body.get("approval_id", ""))
            if body.get("confirmation") != approval_id or not approval_id:
                raise ValueError("请输入完整审批 ID 确认")
            row = self.store._connection.execute(
                "SELECT 1 FROM approvals WHERE task_id=? AND approval_id=?", (task_id, approval_id)
            ).fetchone()
            manager = self.engine.approvals
            decision = body.get("decision")
            if not row or not manager or decision not in {"approve", "deny"}:
                raise ValueError("审批请求无效")
            manager.decide(approval_id, decision, manager.decision_token(approval_id, decision), source="local-web")
            if decision == "approve":
                # Preserve the engine's native permission revalidation / BLOCKED behavior.
                await self.engine.run_task(task_id)
        else:
            raise ValueError("操作不存在")
        return self.detail(task_id)


async def serve(engine: TaskEngine, store: Store, port: int, *, open_browser: bool = True) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1–65535 之间")
    app = WebApplication(engine, store)
    assets = Path(__file__).with_name("web")

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        status, content_type = 200, "application/json; charset=utf-8"
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            lines = header.decode("ascii").split("\r\n")
            method, path, _ = lines[0].split(" ")
            headers = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
            headers = {k.lower(): v.strip() for k, v in headers.items()}
            host = headers.get("host", "")
            if host != f"127.0.0.1:{port}":
                raise ValueError("Host 不允许")
            if headers.get("origin", f"http://{host}") != f"http://{host}":
                raise ValueError("跨域请求不允许")
            if method == "POST" and not secrets.compare_digest(headers.get("x-agent-center-token", ""), app.token):
                raise ValueError("请求令牌无效，请刷新页面")
            length = int(headers.get("content-length", "0"))
            if length < 0 or length > 65536 or "transfer-encoding" in headers:
                raise ValueError("请求体过大或不支持")
            raw = await asyncio.wait_for(reader.readexactly(length), 5) if length else b"{}"
            if method == "GET" and path in {"/", "/app.js", "/style.css"}:
                name = "index.html" if path == "/" else path[1:]
                data = (assets / name).read_bytes().replace(b"__CSRF_TOKEN__", app.token.encode())
                content_type = {"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}[name]
                content_type += "; charset=utf-8"
            else:
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError("请求必须是对象")
                data = json.dumps(redact_value(await app.api(method, path, body)), ensure_ascii=False).encode()
        except Exception as exc:
            status = 400
            data = json.dumps({"error": redact_text(str(exc))}, ensure_ascii=False).encode()
        writer.write(
            (
                f"HTTP/1.1 {status} {'OK' if status == 200 else 'Bad Request'}\r\n"
                f"Content-Type: {content_type}\r\nContent-Length: {len(data)}\r\n"
                "Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n"
                "Content-Security-Policy: default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "frame-ancestors 'none'; object-src 'none'\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            + data
        )
        with contextlib.suppress(ConnectionError):
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", port, limit=65536)
    url = f"http://127.0.0.1:{port}"
    print(f"Agent Center Web: {url}", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        async with server:
            await server.serve_forever()
    finally:
        for cancel, _ in app.controls.values():
            cancel.set()
        for job in app.jobs.values():
            if not job.done():
                job.cancel()
        await asyncio.gather(*app.jobs.values(), return_exceptions=True)

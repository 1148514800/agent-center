"""Loopback-only browser UI. All state and execution remain owned by TaskEngine."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import webbrowser
from pathlib import Path
from typing import Any

from agent_center.config import AppConfiguration
from agent_center.domain import TaskStatus
from agent_center.engine import TaskEngine
from agent_center.runtime.identity import verify_process_identity
from agent_center.storage.database import Store
from agent_center.storage.redaction import redact_text, redact_value


class WebApplication:
    def __init__(self, engine: TaskEngine, store: Store, config_path: Path | None = None):
        self.config_path = config_path
        self.engine = engine
        self.store = store
        self.token = secrets.token_urlsafe(32)
        self.jobs: dict[str, asyncio.Task[None]] = {}
        self.controls: dict[str, tuple[asyncio.Event, asyncio.Event]] = {}

    def running(self, task_id: str) -> bool:
        return task_id in self.jobs and not self.jobs[task_id].done()

    def recovery(self, task_id: str) -> dict[str, Any]:
        task = self.store.task(task_id)
        assert task is not None
        runs = self.store.runs(task_id)
        incomplete = [
            r for r in runs if r["status"] in {"prepared", "starting", "running", "unknown"}
            or (r["status"] in {"failed", "cancelled", "timed_out"} and not r["event_complete"])
        ]
        approval = self.store._connection.execute(
            "SELECT status FROM approvals WHERE task_id=? AND approval_id=?",
            (task_id, task["pending_approval_id"]),
        ).fetchone()
        pending_approval = bool(approval and approval["status"] in {"pending", "approved"})
        needs_review = bool(incomplete) or task["status"] in {"blocked", "interrupted", "running", "verifying"}
        needs_review = needs_review or (
            task["status"] == "planning" and not self.running(task_id)
            and task["last_error"] != "Prior effects reviewed; create a fresh plan before any new Run"
        )
        needs_review = needs_review or bool(task["pending_approval_id"] and not pending_approval)
        needs_review = needs_review or (task["status"] == "waiting_approval" and not pending_approval)
        return {
            "needs_review": needs_review and not self.running(task_id),
            "can_resume": not self.running(task_id) and not incomplete and not needs_review and not pending_approval
            and task["status"] in {"created", "paused", "planning"},
            "can_reconcile": not self.running(task_id) and not pending_approval,
            "hint": "请先在任务详情处理审批请求，再检查与对账。" if pending_approval else (
                "请检查 Run、日志路径和工作区变化，确认进程已停止后对账。"
                "对账后重新规划，旧 Run 不会重放。"
            ),
        }

    def configuration_guard(self, *, adding: bool = False) -> None:
        from agent_center.setup import ensure_configuration_editable

        if any(not job.done() for job in self.jobs.values()):
            raise ValueError("当前任务正在运行；请打开任务并暂停或停止后再修改配置")
        ensure_configuration_editable(self.store, adding=adding)

    def update_configuration(self, config: AppConfiguration) -> None:
        self.engine.config = config
        self.engine.model.config = config
        self.engine.verifier.config = config
        active = self.store.active_task()
        if active:
            self.store.append_event(active["task_id"], "connection_configuration_updated", {
                "reason": "Local user changed idle connection settings; existing Run snapshots are preserved",
            })

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
            "running": self.running(task_id),
            "recovery": self.recovery(task_id),
        }

    def start(self, task_id: str) -> None:
        if any(not job.done() for job in self.jobs.values()):
            raise ValueError("已有任务正在运行")
        if self.store.unresolved_runs():
            raise ValueError("存在未对账的 Run，请打开对应任务，检查进程与工作区后对账")
        task = self.store.task(task_id)
        if not task or not self.recovery(task_id)["can_resume"]:
            raise ValueError("此任务需要先检查阻塞原因，并在任务详情中确认对账后继续")
        active = self.store.active_task()
        if active and active["task_id"] != task_id:
            raise ValueError(f"请先处理当前任务 {active['task_id']}")
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
        if method == "GET" and path == "/api/agents/health":
            from agent_center.capabilities import installation_identity, required_capabilities

            result = {}
            for name, agent in self.engine.config.agents.items():
                profile = self.engine.config.profiles[agent.default_profile]
                identity = installation_identity(agent, profile)
                required = required_capabilities(profile.permission_mode)
                if not identity:
                    status, reason = "不可用", "未找到或无法读取 CLI / 安装包，请检查程序位置"
                elif not agent.enabled:
                    status, reason = "不可用", "Agent 已停用"
                elif any(getattr(agent.capabilities, key) == "UNSUPPORTED" for key in required):
                    status, reason = "不可用", "原生 CLI 不支持当前权限所需的能力，请查看详情"
                elif not agent.capabilities.permits(identity, required):
                    status, reason = "未验证", "所需能力尚未验证，或安装 / 配置变化使旧证据失效"
                elif not agent.allow_execution:
                    status, reason = "已验证", "能力已验证，配置中的执行开关仍关闭"
                else:
                    status, reason = "已验证", "本机能力与当前安装匹配，允许调度"
                result[name] = {"status": status, "reason": reason}
            return result
        if method == "POST" and path == "/api/supervisor":
            from agent_center.setup import register_supervisor

            if self.config_path is None:
                raise ValueError("未指定 Agent Center 配置位置")
            self.configuration_guard()
            values = {k: body[k] for k in (
                "protocol", "base_url_env", "api_key_env", "model", "reasoning_effort",
                "request_timeout_seconds", "max_output_tokens",
            ) if k in body}
            raw = self.engine.config.model_dump(mode="json")
            raw["supervisor"] = values
            candidate = AppConfiguration.model_validate(raw)
            base_url, api_key = body.get("base_url", ""), body.get("api_key", "")
            if not isinstance(base_url, str) or not isinstance(api_key, str) or len(api_key) > 8192:
                raise ValueError("统筹连接信息格式不正确")
            # Validate prospective environment values before persisting anything.
            from agent_center.config import ProfileConfig

            ProfileConfig(agent_id="codex", model="", base_url=base_url)
            updated = register_supervisor(self.engine.config, self.config_path, values)
            if base_url.strip():
                os.environ[candidate.supervisor.base_url_env] = base_url.strip().rstrip("/")
            if api_key:
                os.environ[candidate.supervisor.api_key_env] = api_key
            self.update_configuration(updated)
            return {"saved": True}
        if method == "POST" and path == "/api/agents/models":
            from agent_center.models import discover_models

            if body.get("api_key_env") in {
                self.engine.config.supervisor.api_key_env,
                self.engine.config.supervisor.base_url_env,
            }:
                raise ValueError("不能向 Agent 模型服务传递统筹凭证")
            # Metadata discovery only; never creates a task or grants execution permissions.
            return await discover_models(body)
        if method == "POST" and path == "/api/directories":
            from agent_center.setup import browse_directory

            return browse_directory(
                str(body.get("path", "")), files=body.get("files") is True, hidden=body.get("hidden") is True
            )
        if method == "GET" and path == "/api/agents/discover":
            from agent_center.setup import discover_agents

            return discover_agents()
        if method == "POST" and path == "/api/agents":
            from agent_center.setup import register_agent

            if self.config_path is None:
                raise ValueError("未指定 Agent Center 配置位置")
            existing = self.engine.config.agents.get(str(body.get("agent_id", "")))
            adding = existing is None or "REPLACE_WITH" in self.engine.config.profiles[existing.default_profile].model
            self.configuration_guard(adding=adding)
            values = {
                k: body[k]
                for k in (
                    "agent_id",
                    "executable",
                    "model",
                    "provider",
                    "cli_install_dir",
                    "permission_mode",
                    "base_url",
                    "api_key_env",
                    "reasoning_effort",
                )
                if k in body
            }
            api_key = body.get("api_key", "")
            if not isinstance(api_key, str) or len(api_key) > 8192:
                raise ValueError("API Key 格式不正确")
            if api_key and (not values.get("base_url") or not values.get("api_key_env")):
                raise ValueError("填写密钥时请选择自定义 API 地址和密钥变量")
            updated = register_agent(
                self.engine.config, self.config_path, values, replace=body.get("replace") is True
            )
            if api_key:
                os.environ[str(values["api_key_env"])] = api_key
            self.update_configuration(updated)
            return {"saved": True, "execution_enabled": False}
        if method == "GET" and path == "/api/state":
            config = self.engine.config
            return {
                "api_revision": 3,
                "tasks": self.store.list_tasks(100),
                "active_task_id": (self.store.active_task() or {}).get("task_id"),
                "unresolved_task_ids": list(dict.fromkeys(r["task_id"] for r in self.store.unresolved_runs())),
                "agents": [
                    {
                        "id": name,
                        "enabled": a.enabled,
                        "execution_enabled": a.allow_execution,
                        "executable": a.executable,
                        "cli_install_dir": a.cli_install_dir,
                        "model": config.profiles[a.default_profile].model,
                        "permission": config.profiles[a.default_profile].permission_mode,
                        "capabilities": a.capabilities.model_dump(mode="json"),
                    }
                    for name, a in config.agents.items()
                ],
                "limits": config.limits.model_dump(),
                "supervisor": config.supervisor.model,
                "supervisor_settings": {
                    **config.supervisor.model_dump(exclude={"api_key_env"}),
                    "key_variable": config.supervisor.api_key_env,
                    "address_configured": bool(os.environ.get(config.supervisor.base_url_env, "")),
                    "key_configured": bool(os.environ.get(config.supervisor.api_key_env, "")),
                },
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
            if self.recovery(task_id)["can_resume"]:
                try:
                    self.start(task_id)
                except ValueError as exc:
                    self.store.set_status(task_id, TaskStatus.BLOCKED, error=redact_text(str(exc)))
        elif action == "resume":
            self.start(task_id)
        elif action == "reconcile":
            if body.get("confirmation") != task_id or body.get("reviewed") is not True:
                raise ValueError("请确认已检查工作区变化，并输入完整任务 ID 对账")
            if not self.recovery(task_id)["can_reconcile"]:
                raise ValueError("请先停止当前运行或处理待审批请求")
            for run in self.store.runs(task_id):
                if run["status"] not in {
                    "prepared", "starting", "running", "unknown", "failed", "cancelled", "timed_out",
                }:
                    continue
                alive = verify_process_identity(
                    int(run["pid"] or 0), str(run["executable_path"] or ""),
                    str(run["process_start_identity"] or ""), str(run["process_command_line"] or ""),
                )
                if alive is not False:
                    raise ValueError(
                        f"Run {run['run_id']} 的进程仍在运行或身份不确定；请先在本机核实并停止，不能对账或重放"
                    )
            self.store.recover_stale_active_period(task_id)
            pending = self.store.task(task_id)
            if pending and pending["pending_approval_id"]:
                self.store.clear_pending_approval(task_id, pending["pending_approval_id"])
            self.store.reconcile_task_runs(task_id, user_confirmed=True)
            self.store.settle_active_period(task_id)
        elif action in {"pause", "stop", "cancel"}:
            if detail["running"]:
                cancel, pause = self.controls[task_id]
                (pause if action == "pause" else cancel).set()
                if action == "cancel":
                    await self.jobs[task_id]
            elif action in {"pause", "stop"}:
                raise ValueError("当前没有网页管理的运行；请检查任务详情并对账后继续或取消")
            if action == "cancel":
                if self.recovery(task_id)["needs_review"] and self.store.runs(task_id):
                    raise ValueError("Run 副作用需要检查；请先在任务详情中对账，再取消")
                if detail["task"]["status"] in {"succeeded", "failed", "cancelled"}:
                    raise ValueError("任务已结束")
                self.store.set_status(task_id, TaskStatus.CANCELLED, error="用户通过网页取消")
                self.store.expire_approvals_for_task(task_id, "task_cancelled")
                self.store.settle_active_period(task_id)
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


async def serve(
    engine: TaskEngine, store: Store, port: int, *, open_browser: bool = True, config_path: Path | None = None
) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1–65535 之间")
    app = WebApplication(engine, store, config_path)
    assets = Path(__file__).with_name("web")
    # Keep frontend and backend from the same startup revision, including during source edits.
    asset_bytes = {name: (assets / name).read_bytes() for name in ("index.html", "app.js", "style.css")}

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
                data = asset_bytes[name].replace(b"__CSRF_TOKEN__", app.token.encode())
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

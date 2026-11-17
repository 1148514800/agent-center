from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import signal
import sys
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from agent_center import __version__
from agent_center.capabilities import installation_identity, required_capabilities
from agent_center.config import (
    AppConfiguration,
    default_config_path,
    load_config,
    offline_default_configuration,
)
from agent_center.domain import TaskStatus
from agent_center.engine import TaskEngine
from agent_center.policy.approvals import ApprovalManager, create_signing_key
from agent_center.runtime.identity import verify_process_identity
from agent_center.storage.database import Store, WorkspaceLock
from agent_center.storage.redaction import redact_text
from agent_center.supervisor import ModelClient

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_BUDGET = 3
EXIT_INPUT = 4
EXIT_BLOCKED = 5
EXIT_INTERRUPT = 130


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="agent-center", description="Local Agent coordinator")
    result.add_argument("--version", action="version", version=f"agent-center {__version__}")
    result.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Central TOML config (default ~/.agent-center/config.toml)",
    )
    result.add_argument("--data-dir", type=Path, default=None, help="Override local state directory")
    sub = result.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="Static local compatibility and configuration checks")
    doctor.add_argument("--live", action="store_true", help="Make one real low-token supervisor API request")
    web = sub.add_parser("web", help="Open the local browser interface")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--no-browser", action="store_true")
    sub.add_parser("agents", help="Show configured agent adapters and compatibility status")
    agent = sub.add_parser("agent", help="Register installed agents without editing TOML")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    add = agent_sub.add_parser("add", help="Add a locally installed CLI agent")
    add.add_argument("agent_id", choices=["codex", "deepseek"])
    add.add_argument("--executable")
    add.add_argument("--model")
    add.add_argument("--reasoning-effort", default="")
    add.add_argument("--base-url", default="")
    add.add_argument("--api-key-env", default="")
    add.add_argument("--provider", default="deepseek-official")
    add.add_argument("--cli-install-dir")
    add.add_argument("--permission-mode", choices=["read-only", "workspace-write"], default="read-only")
    add.add_argument("--replace", action="store_true")
    run = sub.add_parser("run", help="Create one task and execute the coordinator")
    run.add_argument("goal")
    run.add_argument("--workspace", type=Path, help="Existing explicitly selected workspace")
    run.add_argument("--verbose", action="store_true")
    chat = sub.add_parser("chat", help="Interactive continuous conversation")
    chat.add_argument("--workspace", type=Path)
    chat.add_argument("--verbose", action="store_true")
    sub.add_parser("history", help="List task history")
    show = sub.add_parser("show", help="Show one task and its persisted evidence")
    show.add_argument("task_id")
    logs = sub.add_parser("logs", help="Show the event log for one task")
    logs.add_argument("task_id")
    logs.add_argument("--verbose", action="store_true")
    resume = sub.add_parser("resume", help="Inspect/reconcile and continue a persisted task")
    resume.add_argument("task_id")
    resume.add_argument(
        "--confirm-reconcile",
        action="store_true",
        help="Confirm that the visible unresolved Run process was stopped and its workspace changes reviewed",
    )
    cancel_task = sub.add_parser("cancel", help="Cancel a quiescent task after reviewing its state")
    cancel_task.add_argument("task_id")
    cancel_task.add_argument("--yes", action="store_true", required=True)
    approve = sub.add_parser("approve", help="Record explicit one-run user approval for a request")
    approve.add_argument("task_id")
    approve.add_argument("approval_id")
    decision = approve.add_mutually_exclusive_group(required=True)
    decision.add_argument("--yes", action="store_true")
    decision.add_argument("--no", action="store_true")
    return result


def _load_configuration(args: argparse.Namespace) -> AppConfiguration:
    path = args.config or default_config_path()
    config = load_config(path)
    if args.data_dir:
        config = config.model_copy(update={"app": config.app.model_copy(update={"data_dir": str(args.data_dir)})})
    return config


def _doctor(config: AppConfiguration, config_path: Path, *, live: bool) -> int:
    print(f"Agent Center {__version__}")
    print(f"Platform: {sys.platform}; Python {sys.version.split()[0]}; SQLite {__import__('sqlite3').sqlite_version}")
    print(f"Config: {config_path.expanduser()} ({'readable' if config_path.expanduser().is_file() else 'missing'})")
    failures = 0
    if sys.version_info < (3, 14) or sys.version_info >= (3, 15):
        print("Python runtime: BLOCKED (requires Python >=3.14,<3.15)")
        failures += 1
    else:
        print("Python runtime: supported version range")
    if sys.platform != "darwin":
        print("Platform compatibility: UNVERIFIED (only macOS is a v0.1 target)")
    if "REPLACE_WITH" in config.supervisor.model:
        print("Supervisor model: NOT_CONFIGURED (placeholder)")
        failures += 1
    else:
        print(f"Supervisor protocol/model: {config.supervisor.protocol} / {config.supervisor.model}")
    for agent_id, agent in config.agents.items():
        executable = shutil.which(agent.executable) or agent.executable
        version = "not queried"
        if agent.adapter == "codex_exec" and Path(executable).is_file():
            version = _bounded_command([executable, "--version"])
        elif agent.adapter == "deepseek_headless" and Path(executable).is_file():
            version = "Desktop launcher exists; headless help was statically verified separately"
        profile = config.profiles[agent.default_profile]
        current = agent.capabilities.permits(
            installation_identity(agent, profile), required_capabilities(profile.permission_mode)
        )
        state = "SUPPORTED" if current else "UNVERIFIED"
        print(
            f"Agent {agent_id}: configured executable={executable}; version={version}; "
            f"capability={state}; execution_enabled={agent.allow_execution}"
        )
        print("  Native capability matrix: " + json.dumps(agent.capabilities.model_dump(mode="json")))
        if not current or not agent.allow_execution:
            print("  Execution remains blocked by local config until safety/protocol checks are completed.")
    print(
        "Permission: full-access/global install/publish values are schema-forced false; "
        "CLI sandbox boundaries require real task probes."
    )
    if live:
        print("doctor --live sends one low-token supervisor request and may incur provider cost.")
        try:
            asyncio.run(_live_probe(config))
        except Exception as exc:
            print(f"Supervisor API probe: FAIL ({type(exc).__name__})")
            failures += 1
        else:
            print("Supervisor API probe: PASS (one request completed; model answer contents not retained).")
    else:
        print("Live CLI/API tests: NOT_RUN; doctor is static and never makes a provider call.")
    return EXIT_BLOCKED if failures else EXIT_OK


async def _live_probe(config: AppConfiguration) -> None:
    if "REPLACE_WITH" in config.supervisor.model:
        raise ValueError("Supervisor model is still a placeholder")
    client = ModelClient(config)
    await client.answer_question(
        system="Reply with the exact text OK. Do not use tools.",
        user="Connectivity probe only; reply OK.",
    )


def _bounded_command(argv: list[str]) -> str:
    import subprocess

    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(Path.home()),
                "LANG": "en_US.UTF-8",
                "TERM": "dumb",
            },
        )
        output = (result.stdout + result.stderr).strip().splitlines()
        return redact_text(output[0][:160] if output else f"exit={result.returncode}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"probe failed ({type(exc).__name__})"


def _agents(config: AppConfiguration) -> None:
    for agent_id, agent in config.agents.items():
        profile = config.profiles[agent.default_profile]
        resolved = shutil.which(agent.executable) or agent.executable
        print(f"{agent_id}: adapter={agent.adapter} executable={resolved}")
        requested = f"{profile.provider + '/' if profile.provider else ''}{profile.model}"
        print(
            f"  requested model={requested}; permission={profile.permission_mode}; "
            f"enabled={agent.enabled and agent.allow_execution}"
        )
        print("  Native capability matrix: " + json.dumps(agent.capabilities.model_dump(mode="json")))
        print("  confirmed model=unknown; auth=unverified; safety boundaries require installation-bound evidence")


def _store_and_engine(config: AppConfiguration) -> tuple[Store, TaskEngine, WorkspaceLock]:
    data_dir = Path(config.app.data_dir).expanduser().resolve()
    lock = WorkspaceLock(data_dir / "runtime.lock")
    lock.__enter__()
    store = Store(data_dir)
    key = create_signing_key(data_dir)
    approvals = ApprovalManager(store, key)
    store.mark_unknown_runs()
    return store, TaskEngine(config, store, approval_manager=approvals), lock


def _show_task(store: Store, task_id: str) -> int:
    task = store.task(task_id)
    if not task:
        print(f"Task not found: {task_id}", file=sys.stderr)
        return EXIT_CONFIG
    view = {
        key: task[key]
        for key in (
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
    }
    view["plan"] = json.loads(task["plan_json"])
    view["evidence"] = store.evidence(task_id)
    view["runs"] = store.runs(task_id)
    print(json.dumps(view, ensure_ascii=False, indent=2))
    return EXIT_OK


async def _run_command(engine: TaskEngine, store: Store, goal: str, workspace: Path | None, verbose: bool) -> int:
    task_id, task_workspace = engine.create_task(goal, workspace)
    print(f"Task: {task_id}\nWorkspace: {task_workspace}")

    async def progress(event: dict[str, Any]) -> None:
        if verbose or event.get("type") != "action":
            print(json.dumps(event, ensure_ascii=False), flush=True)
        else:
            print(f"[{event.get('action')}] {event.get('reason', '')}", flush=True)

    cancel = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, cancel.set)
    try:
        answer = await engine.run_task(task_id, progress=progress, cancel_event=cancel)
        print(answer)
        status = store.task(task_id)
        if status and status["status"] == "succeeded":
            return EXIT_OK
        if status and status["status"] == "waiting_approval":
            return EXIT_INPUT
        if status and status["status"] == "blocked":
            return EXIT_BLOCKED
        return EXIT_OK if status and status["status"] == "succeeded" else EXIT_ERROR
    except RuntimeError as exc:
        print(f"Blocked: {redact_text(str(exc))}", file=sys.stderr)
        return EXIT_BUDGET if "budget exhausted" in str(exc).lower() else EXIT_BLOCKED
    except asyncio.CancelledError:
        store.set_status(
            task_id,
            TaskStatus.INTERRUPTED,
            error="User interrupted; run reconciliation required",
        )
        return EXIT_INTERRUPT


async def _chat(engine: TaskEngine, store: Store, workspace: Path | None, verbose: bool) -> int:
    session: PromptSession[str] = PromptSession()
    current_task: str | None = None
    run_task: asyncio.Task[str] | None = None
    cancel = asyncio.Event()
    pause = asyncio.Event()
    print("Chat: enter a request; /status /plan /logs /ask TEXT /add TEXT /stop /pause /resume /quit")

    async def progress(event: dict[str, Any]) -> None:
        msg = event.get("reason") or event.get("type") or "progress"
        if verbose or event.get("type") not in {"action"}:
            print(f"\n[{event.get('type')}] {json.dumps(event, ensure_ascii=False)}")
        else:
            print(f"\n[{event.get('action')}] {msg}")

    async def start_task(goal: str) -> None:
        nonlocal current_task, run_task, cancel, pause
        current_task, task_workspace = engine.create_task(goal, workspace)
        print(f"Task {current_task}; workspace {task_workspace}")
        cancel = asyncio.Event()
        pause = asyncio.Event()
        run_task = asyncio.create_task(
            engine.run_task(current_task, progress=progress, cancel_event=cancel, pause_event=pause)
        )

    while True:
        try:
            with patch_stdout():
                text = (await session.prompt_async("you> ")).strip()
        except EOFError, KeyboardInterrupt:
            text = "/quit"
        if not text:
            continue
        if text == "/quit":
            if run_task and not run_task.done():
                cancel.set()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await run_task
            return EXIT_OK
        if current_task is None and not text.startswith("/"):
            await start_task(text)
            continue
        if current_task is None:
            print("Start with a task request first.")
            continue
        task = store.task(current_task)
        if task and task["status"] in {"succeeded", "failed", "cancelled"}:
            if not text.startswith("/"):
                current_task = None
                await start_task(text)
            else:
                print("This task is terminal. Start a new task with plain text to continue.")
            continue
        if text == "/status":
            print(
                json.dumps(
                    {
                        key: task[key]
                        for key in (
                            "task_id",
                            "status",
                            "requirements_version",
                            "agent_calls",
                            "supervisor_calls",
                            "last_error",
                        )
                    }
                    if task
                    else {},
                    ensure_ascii=False,
                )
            )
        elif text == "/plan":
            print(task["plan_json"] if task else "task missing")
        elif text == "/logs":
            print(json.dumps(store.events(current_task)[-50:], ensure_ascii=False, indent=2))
        elif text.startswith("/add "):
            message = text[5:].strip()
            store.add_message(current_task, message)
            message_count = len(store.messages(current_task))
            print(f"Received and persisted; applies at the next run boundary (message #{message_count}).")
        elif text.startswith("/ask "):
            try:
                print(await engine.process_question(current_task, text[5:].strip()))
            except Exception as exc:
                print(f"Read-only question failed: {redact_text(str(exc))}")
        elif text == "/stop":
            cancel.set()
            if run_task and not run_task.done():
                with contextlib.suppress(Exception):
                    await run_task
            print("Stopped; task preserved for reconciliation.")
        elif text == "/cancel":
            cancel.set()
            if run_task and not run_task.done():
                with contextlib.suppress(Exception):
                    await run_task
            store.set_status(
                current_task,
                TaskStatus.CANCELLED,
                error="Cancelled by user",
            )
            print("Task cancelled; start a new task for further requirements.")
        elif text == "/pause":
            pause.set()
            print("Pause requested; it takes effect after the current isolated CLI run finishes.")
        elif text == "/resume":
            if task and task["status"] == "paused" and run_task and run_task.done():
                store.set_status(current_task, TaskStatus.PLANNING)
                pause = asyncio.Event()
                run_task = asyncio.create_task(
                    engine.run_task(current_task, progress=progress, cancel_event=cancel, pause_event=pause)
                )
                print("Resumed at a saved task boundary; the previous CLI process is not reused.")
            else:
                print("Task cannot resume yet; inspect status and reconcile any unresolved run.")
        elif text.startswith("/approve "):
            print("Use `agent-center approve TASK_ID APPROVAL_ID --yes` after reviewing its persisted scope.")
        elif not text.startswith("/"):
            message = store.add_message(current_task, text)
            print(f"Received and persisted as {message}; applies after the active Run boundary.")
        else:
            print("Unknown command.")
        if run_task and run_task.done():
            try:
                answer = run_task.result()
                if answer:
                    print(f"agent-center> {answer}")
            except Exception as exc:
                print(f"Task paused/blocked: {redact_text(str(exc))}")


def main() -> None:
    args = parser().parse_args()
    config_path = args.config or default_config_path()
    try:
        config = _load_configuration(args)
    except Exception as exc:
        if args.command in {"doctor", "web", "agent"} and not config_path.expanduser().exists():
            print(f"Configuration: NOT_CONFIGURED ({type(exc).__name__})")
            config = offline_default_configuration()
        elif args.command == "agents":
            print(f"Configuration: NOT_CONFIGURED ({type(exc).__name__})", file=sys.stderr)
            config = offline_default_configuration()
        else:
            print(f"Configuration error: {type(exc).__name__}: {redact_text(str(exc))}", file=sys.stderr)
            raise SystemExit(EXIT_CONFIG) from exc
    if not config_path.expanduser().exists():
        from agent_center.setup import apply_registrations

        config = apply_registrations(config, config_path)
    if args.command == "agent":
        from agent_center.setup import discover_agents, ensure_configuration_editable, register_agent

        found = next((a for a in discover_agents() if a["agent_id"] == args.agent_id), {})
        executable = args.executable or found.get("executable", "")
        model = args.model or ""
        package = args.cli_install_dir or found.get("cli_install_dir", "")
        if sys.stdin.isatty():
            executable = input(f"CLI executable [{executable}]: ").strip() or executable
            if not model:
                from agent_center.models import discover_models

                try:
                    catalog = asyncio.run(
                        discover_models(
                            {
                                "agent_id": args.agent_id,
                                "executable": executable,
                                "cli_install_dir": package,
                                "base_url": args.base_url,
                                "api_key_env": args.api_key_env,
                            }
                        )
                    )["models"]
                    for index, item in enumerate(catalog, 1):
                        print(f"{index}. {item['name']} ({item['id']})")
                    selection = input("Model number / ID (blank = CLI default): ").strip()
                    selected = (
                        catalog[int(selection) - 1]
                        if selection.isdigit() and 1 <= int(selection) <= len(catalog)
                        else None
                    )
                    model = selected["id"] if selected else selection
                    if selected and selected.get("efforts") and not args.reasoning_effort:
                        print("Reasoning choices: " + ", ".join(selected["efforts"]))
                        effort = input("Reasoning effort (blank = model default): ").strip()
                        if effort and effort not in selected["efforts"]:
                            raise ValueError("Selected model does not support this reasoning effort")
                        args.reasoning_effort = effort
                except (OSError, ValueError, TimeoutError) as exc:
                    print(f"Model discovery unavailable: {type(exc).__name__}")
                    model = input("Model ID (Codex subscription: blank = default): ").strip()
            if args.agent_id == "deepseek":
                package = input(f"Harness package directory [{package}]: ").strip() or package
        try:
            data_dir = Path(args.data_dir or config.app.data_dir).expanduser()
            with WorkspaceLock(data_dir / "runtime.lock"):
                setup_store = Store(data_dir)
                try:
                    existing = config.agents.get(args.agent_id)
                    adding = existing is None or "REPLACE_WITH" in config.profiles[existing.default_profile].model
                    ensure_configuration_editable(setup_store, adding=adding)
                    register_agent(
                        config,
                        config_path,
                        {
                            "agent_id": args.agent_id,
                            "executable": executable,
                            "model": model,
                            "provider": args.provider if args.agent_id == "deepseek" else "",
                            "cli_install_dir": package,
                            "permission_mode": args.permission_mode,
                            "base_url": args.base_url,
                            "api_key_env": args.api_key_env,
                            "reasoning_effort": args.reasoning_effort,
                        },
                        replace=args.replace,
                    )
                finally:
                    setup_store.close()
        except (ValueError, OSError, RuntimeError) as exc:
            print(f"Registration failed: {exc}", file=sys.stderr)
            raise SystemExit(EXIT_CONFIG) from exc
        print(f"Registered {args.agent_id}. Execution remains disabled until native capabilities are verified.")
        return
    if args.data_dir:
        config = config.model_copy(update={"app": config.app.model_copy(update={"data_dir": str(args.data_dir)})})
    if args.command == "doctor":
        raise SystemExit(_doctor(config, config_path, live=args.live))
    if args.command == "agents":
        _agents(config)
        raise SystemExit(EXIT_OK)
    try:
        store, engine, lock = _store_and_engine(config)
    except (OSError, RuntimeError) as exc:
        print(f"Cannot open local state: {redact_text(str(exc))}", file=sys.stderr)
        raise SystemExit(EXIT_BLOCKED) from exc
    try:
        if args.command == "web":
            from agent_center.web_server import serve

            try:
                asyncio.run(serve(engine, store, args.port, open_browser=not args.no_browser, config_path=config_path))
            except KeyboardInterrupt:
                print("Web server stopped; task state preserved.")
            return
        if args.command == "history":
            print(json.dumps(store.list_tasks(), ensure_ascii=False, indent=2))
            raise SystemExit(EXIT_OK)
        if args.command == "show":
            raise SystemExit(_show_task(store, args.task_id))
        if args.command == "logs":
            for event in store.events(args.task_id):
                if args.verbose:
                    print(json.dumps(event, ensure_ascii=False))
                else:
                    print(f"{event['sequence']:04d} {event['type']}")
            raise SystemExit(EXIT_OK)
        if args.command == "cancel":
            if not sys.stdin.isatty():
                print("Interactive confirmation is required.", file=sys.stderr)
                raise SystemExit(EXIT_INPUT)
            confirmation = input(f"Type the task ID {args.task_id} to confirm cancellation: ").strip()
            if confirmation != args.task_id:
                raise SystemExit(EXIT_INPUT)
            task = store.task(args.task_id)
            if task is None:
                print("Task not found.", file=sys.stderr)
                raise SystemExit(EXIT_CONFIG)
            active_runs = [
                run for run in store.runs(args.task_id) if run["status"] in {"prepared", "starting", "running"}
            ]
            if active_runs:
                print(
                    "Cannot mark the task cancelled while a Run is active. Use the chat /stop control first.",
                    file=sys.stderr,
                )
                raise SystemExit(EXIT_BLOCKED)
            if task["status"] not in {"created", "paused", "blocked", "interrupted"}:
                print(f"Task status {task['status']} cannot be cancelled by this command.", file=sys.stderr)
                raise SystemExit(EXIT_BLOCKED)
            store.set_status(
                args.task_id,
                TaskStatus.CANCELLED,
                error="Explicit user cancellation",
            )
            print(f"Task {args.task_id} marked cancelled; prior Runs and possible side effects remain in history.")
            raise SystemExit(EXIT_OK)
        if args.command == "approve":
            approval = store._connection.execute(
                "SELECT target_path,operation,risk,requirements_version FROM approvals "
                "WHERE approval_id=? AND task_id=?",
                (args.approval_id, args.task_id),
            ).fetchone()
            if not approval:
                print("Approval not found.", file=sys.stderr)
                raise SystemExit(EXIT_CONFIG)
            print(
                f"Approval scope: {approval[1]} at {approval[0]} (directory-level)\n"
                f"Risk: {approval[2]}\nRequirements version: {approval[3]}"
            )
            if not sys.stdin.isatty():
                print("Interactive approval is required.", file=sys.stderr)
                raise SystemExit(EXIT_INPUT)
            response = input("Type the exact approval ID to confirm this one-run scope: ").strip()
            if response != args.approval_id:
                raise SystemExit(EXIT_INPUT)
            manager = engine.approvals
            assert manager is not None
            decision = "approve" if args.yes else "deny"
            if manager.decide(
                args.approval_id,
                decision,
                manager.decision_token(args.approval_id, decision),
                source="local_cli",
            ):
                print(
                    "Approval record accepted for this scope/version. It does not grant permissions "
                    "the backend cannot enforce."
                )
            else:
                print("Denied, expired, or scope changed.")
            raise SystemExit(EXIT_OK)
        if args.command == "run":
            code = asyncio.run(_run_command(engine, store, args.goal, args.workspace, args.verbose))
            raise SystemExit(code)
        if args.command == "chat":
            code = asyncio.run(_chat(engine, store, args.workspace, args.verbose))
            raise SystemExit(code)
        if args.command == "resume":
            task = store.task(args.task_id)
            if not task:
                print("Task not found.", file=sys.stderr)
                raise SystemExit(EXIT_CONFIG)
            runs = store.runs(args.task_id)
            unresolved = [r for r in runs if r["status"] in {"prepared", "starting", "running", "unknown"}]
            if unresolved:
                print("Unresolved Run records require process/workspace reconciliation:")
                for run in unresolved:
                    alive = verify_process_identity(
                        int(run["pid"] or 0),
                        str(run["executable_path"] or ""),
                        str(run["process_start_identity"] or ""),
                        str(run["process_command_line"] or ""),
                    )
                    process_label = (
                        "alive-and-matched"
                        if alive is True
                        else ("not-running" if alive is False else "identity-ambiguous")
                    )
                    if alive is True:
                        print(
                            "The matching child process is still alive; do not reconcile while it can write.",
                            file=sys.stderr,
                        )
                        raise SystemExit(EXIT_BLOCKED)
                    if alive is None:
                        print(
                            "Could not establish the old PID identity; refusing reconciliation and replay.",
                            file=sys.stderr,
                        )
                        raise SystemExit(EXIT_BLOCKED)
                    print(
                        f"{run['run_id']} status={run['status']} pid={run['pid']} "
                        f"verified_process={process_label} log={run['log_path']}"
                    )
                if not args.confirm_reconcile or not sys.stdin.isatty():
                    print(
                        "No process was killed. Stop only a positively identified Agent process, "
                        "review its workspace side effects, then rerun with --confirm-reconcile."
                    )
                    raise SystemExit(EXIT_BLOCKED)
                confirmation = input(
                    "Type the exact task ID after confirming the old process is stopped and "
                    "workspace effects are reviewed: "
                ).strip()
                if confirmation != args.task_id:
                    raise SystemExit(EXIT_INPUT)
                store.reconcile_task_runs(args.task_id, user_confirmed=True)
                runs = store.runs(args.task_id)
            task = store.task(args.task_id)
            pending_approval = store.pending_approval(args.task_id)
            if pending_approval is not None:
                approval_id, _pending_action = pending_approval
                approval_row = store._connection.execute(
                    "SELECT status FROM approvals WHERE approval_id=? AND task_id=?",
                    (approval_id, args.task_id),
                ).fetchone()
                if approval_row is None or approval_row[0] not in {"approved", "denied", "expired"}:
                    print("No matching approval decision has been recorded.", file=sys.stderr)
                    raise SystemExit(EXIT_INPUT)
                if approval_row[0] != "approved":
                    store.clear_pending_approval(args.task_id, approval_id)
                    print(f"Approval was {approval_row[0]}; the task remains paused without a permission grant.")
                    raise SystemExit(EXIT_BLOCKED)
                task = store.task(args.task_id)
                if task is None:
                    raise SystemExit(EXIT_CONFIG)
                current_snapshot = engine._config_snapshot()
                if not engine.approvals or engine.approvals.config_digest(
                    current_snapshot
                ) != engine.approvals.config_digest(json.loads(task["config_snapshot"])):
                    store.expire_approvals_for_task(args.task_id, "task_config_changed")
                    store.clear_pending_approval(args.task_id, approval_id)
                    store.set_status(args.task_id, TaskStatus.BLOCKED, error="Approval configuration changed")
                    print("Approval configuration snapshot changed; request expired.", file=sys.stderr)
                    raise SystemExit(EXIT_BLOCKED)
                approval_version = store._connection.execute(
                    "SELECT requirements_version FROM approvals WHERE approval_id=?", (approval_id,)
                ).fetchone()
                if approval_version is None or int(task["requirements_version"]) != int(approval_version[0]):
                    store.expire_approvals_for_task(args.task_id, "requirements_changed")
                    store.clear_pending_approval(args.task_id, approval_id)
                    store.set_status(args.task_id, TaskStatus.BLOCKED, error="Approval requirements changed")
                    print("Approval requirements version changed; request expired.", file=sys.stderr)
                    raise SystemExit(EXIT_BLOCKED)
                store.expire_approvals_for_task(args.task_id, "backend_grant_unavailable")
                store.clear_pending_approval(args.task_id, approval_id)
                store.set_status(
                    args.task_id,
                    TaskStatus.BLOCKED,
                    error="The configured backend cannot enforce the approved narrow scope",
                )
                # Approval is revalidated and logged; adapters still cannot apply a narrower grant.
                print(
                    "Approval decision recorded, but backend authorization remains unavailable; "
                    "no privileged action is replayed."
                )
                raise SystemExit(EXIT_BLOCKED)
            if any(run["status"] == "unknown" for run in runs):
                print(
                    "An unknown Run remains non-replayable even after operator reconciliation. "
                    "Archive or manually resolve it before starting any new work.",
                    file=sys.stderr,
                )
                raise SystemExit(EXIT_BLOCKED)
            if task and task["status"] not in {"paused", "interrupted", "blocked", "planning"}:
                print(f"Task status {task['status']} is not resumable.", file=sys.stderr)
                raise SystemExit(EXIT_BLOCKED)
            if task and task["status"] == "interrupted":
                print("Interrupted task may have external side effects. Resume only after reviewing the workspace.")
                if not args.confirm_reconcile or not sys.stdin.isatty():
                    print("Use --confirm-reconcile and type the task ID to re-plan without replaying the old Run.")
                    raise SystemExit(EXIT_BLOCKED)
                confirmation = input("Type the exact task ID after reviewing workspace state: ").strip()
                if confirmation != args.task_id:
                    raise SystemExit(EXIT_INPUT)
                with store.transaction() as conn:
                    conn.execute(
                        "UPDATE tasks SET plan_json='[]',criteria_json='[]',state_version=state_version+1,"
                        "updated_at=? WHERE task_id=?",
                        (__import__("agent_center.storage.database", fromlist=["now"]).now(), args.task_id),
                    )
                    store.emit(
                        conn,
                        args.task_id,
                        "resume_confirmed_after_interruption",
                        {
                            "requirements_version": task["requirements_version"],
                            "old_run_count": len(runs),
                            "explicit_confirmation": True,
                            "plan_cleared": True,
                        },
                    )
            if task and task["status"] == "paused":
                incomplete_runs = [run for run in runs if not run["event_complete"]]
                if incomplete_runs:
                    print(
                        "Paused task has a Run without a committed completion record; reconcile it first.",
                        file=sys.stderr,
                    )
                    raise SystemExit(EXIT_BLOCKED)
            unresolved_effects = [
                run
                for run in runs
                if run["status"] in {"failed", "cancelled", "timed_out"} and not run["event_complete"]
            ]
            if unresolved_effects:
                print(
                    "Previous Run may have changed the workspace; inspect it and use a new reviewed task.",
                    file=sys.stderr,
                )
                raise SystemExit(EXIT_BLOCKED)
            if task and task["status"] == "blocked":
                print("Blocked task requires fixing its stated blocker before resume.", file=sys.stderr)
                raise SystemExit(EXIT_BLOCKED)
            store.set_status(args.task_id, TaskStatus.PLANNING)

            async def progress(event: dict[str, Any]) -> None:
                print(json.dumps(event, ensure_ascii=False), flush=True)

            result = asyncio.run(engine.run_task(args.task_id, progress=progress))
            print(result)
            final = store.task(args.task_id)
            raise SystemExit(EXIT_OK if final and final["status"] == "succeeded" else EXIT_BLOCKED)
    finally:
        store.close()
        lock.__exit__(None, None, None)


if __name__ == "__main__":
    main()

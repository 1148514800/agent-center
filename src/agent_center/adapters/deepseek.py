from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from agent_center.adapters.base import (
    AgentRequest,
    AgentRunResult,
    CommandExecution,
    LaunchSpec,
    NormalizedEvent,
    parse_jsonl,
    verification_prompt,
)
from agent_center.runtime.process import ProcessResult


class DeepSeekHeadlessAdapter:
    """One-shot headless adapter for the installed Desktop launcher protocol."""

    def build_launch(self, request: AgentRequest) -> LaunchSpec:
        executable = Path(request.config.executable).expanduser().resolve()
        if not executable.is_absolute() or not executable.exists():
            raise FileNotFoundError(f"Configured DeepSeek Harness launcher unavailable: {executable}")
        install_dir = request.config.cli_install_dir.strip()
        if not install_dir:
            raise RuntimeError("Set agents.deepseek.cli_install_dir to the verified installed package path")
        cli_entry = Path(install_dir) / "lib" / "bin.js"
        asar = next((p for p in cli_entry.parents if p.suffix == ".asar"), None)
        if not cli_entry.is_file() and not (asar and asar.is_file()):
            raise RuntimeError("The configured DSH installation does not contain the verified CLI entry")
        if request.profile.permission_mode not in {"read-only", "workspace-write"}:
            raise ValueError("Unsupported DeepSeek permission mode")
        # The agent model is centrally selected through a per-invocation JSON/YAML overlay.
        if "REPLACE_WITH" in request.profile.model:
            raise ValueError("DeepSeek agent model is still a placeholder")
        provider = request.profile.provider
        if not provider:
            raise ValueError("DeepSeek profile must declare the provider route explicitly")
        overlay_rows: list[dict[str, Any]] = [
            {
                "name": "@deepseek-ai/dsh-agent-default-model",
                "config": {"provider": provider, "model": request.profile.model},
            }
        ]
        if request.profile.reasoning_effort:
            overlay_rows[0]["config"]["reasoningEffort"] = request.profile.reasoning_effort
        overlay = request.log_dir / "runtime-overlay.yaml"
        overlay.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # JSON is valid YAML and uses only per-call values; the original DSH profile is never edited.
        overlay.write_text(json.dumps(overlay_rows, ensure_ascii=False), encoding="utf-8")
        os.chmod(overlay, 0o600)
        argv = [
            "--patch",
            str(overlay),
            "--profile",
            "headless",
            "--permission-mode",
            request.profile.permission_mode,
            "--approval-policy",
            "never",
            "--json",
            "-",
        ]
        prompt = (
            f"Task {request.task_id}; requirements version {request.requirements_version}.\n"
            f"Objective: {request.objective}\n\n{request.context}\n\n"
            "Work only in the assigned directory, do not request or claim permission for anything "
            "outside the task scope, do not publish/deploy/push, and report exact checks and exit codes."
        ).encode()
        prompt += verification_prompt(request).encode()
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(Path.home()),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "TERM": "dumb",
            "ELECTRON_RUN_AS_NODE": "1",
            "DSH_INSTALL_DIR": install_dir,
            "DSH_HOME": str(request.log_dir / "isolated-dsh-home"),
        }
        for name in request.profile.credential_env:
            if name in {"DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"} and name in os.environ:
                env[name] = os.environ[name]
        return LaunchSpec(str(executable), tuple([str(cli_entry), *argv]), env, prompt, "dsh-headless-json-v1")

    def parse(self, request: AgentRequest, process: ProcessResult) -> AgentRunResult:
        if process.truncated:
            return AgentRunResult(process, (), False, "log_capacity_exceeded", "")
        try:
            raw_events = parse_jsonl(request.log_dir / "stdout.log")
        except (OSError, ValueError) as exc:
            return AgentRunResult(process, (), False, f"protocol_error: {exc}", "")
        events: list[NormalizedEvent] = []
        calls: dict[str, dict[str, Any]] = {}
        commands: list[CommandExecution] = []
        approvals: list[str] = []
        final_reason: str | None = None
        final_text = ""
        failure = False
        failures: list[str] = []
        session_ids = [str(event.get("sessionId")) for event in raw_events if event.get("type") == "session"]
        seen_calls: set[str] = set()
        final_seen = False
        for raw in raw_events:
            if final_seen:
                failure = True
            kind = str(raw.get("type", "unknown"))
            if raw.get("truncated"):
                failure = True
                failures.append("truncated event")
            if kind == "tool_call":
                call_id = raw.get("callId")
                if not isinstance(call_id, str) or call_id in seen_calls:
                    failure = True
                else:
                    calls[call_id] = raw
                    seen_calls.add(call_id)
                    tool_input = raw.get("input")
                    if isinstance(tool_input, dict) and tool_input.get("sandbox_permissions") not in {
                        None,
                        request.profile.permission_mode,
                    }:
                        approvals.append(
                            "Requested native permission: " + json.dumps(tool_input, ensure_ascii=False)[:1000]
                        )
            if kind == "tool_result":
                call = calls.pop(str(raw.get("callId")), None)
                if call is None:
                    failure = True
                elif call.get("tool") == "bash":
                    args = call.get("input")
                    args = args if isinstance(args, dict) else {}
                    # rc.3 projects model-facing rendered text, not the canonical executor DTO.
                    # Never scrape an exit marker out of text: project stdout can forge it.
                    commands.append(
                        CommandExecution(
                            str(args.get("command", "")),
                            None,
                            None,
                            str(raw.get("status", "unknown")),
                            raw.get("result") if isinstance(raw.get("result"), str) else None,
                            str(raw.get("callId", "")),
                            False,
                        )
                    )
                error_data = raw.get("error")
                if isinstance(error_data, dict) and any(
                    word in json.dumps(error_data).lower() for word in ("approval", "denied", "permission")
                ):
                    approvals.append(json.dumps(error_data)[:1000])
            if kind == "error":
                failure = True
                failures.append(str(raw.get("message", "unspecified protocol error"))[:400])
            if kind == "tool_result" and raw.get("status") != "completed":
                failure = True
                error_obj = raw.get("error")
                failures.append(
                    str(
                        error_obj.get("reason", error_obj.get("code", "tool failed"))
                        if isinstance(error_obj, dict)
                        else raw.get("status", "tool failed")
                    )[:400]
                )
            if kind == "final":
                final_seen = True
                reason = raw.get("reason")
                if isinstance(reason, dict):
                    final_reason = str(reason.get("kind", ""))
                    if final_reason != "completed":
                        failure = True
                final_session = raw.get("sessionId")
                if session_ids and final_session != session_ids[-1]:
                    failure = True
                    failures.append("session identity mismatch")
                answer = raw.get("text")
                if isinstance(answer, str):
                    final_text = answer[:20000]
            summary = _summary(raw)
            events.append(
                NormalizedEvent(
                    kind,
                    summary[:1000],
                    raw,
                    kind == "final",
                    kind == "error" or (kind == "final" and failure),
                )
            )
        completed = (
            process.returncode == 0
            and not process.timed_out
            and not process.cancelled
            and not calls
            and len(session_ids) == 1
            and session_ids[0] not in {"None", ""}
            and final_reason == "completed"
            and not failure
        )
        error = None if completed else _failure(process, final_reason, bool(events))
        if not completed and (calls or failure or len(session_ids) != 1):
            error = "incomplete_or_invalid_protocol"
        if failure and failures:
            error = "internal_tool_failure: " + "; ".join(failures)[:1000]
        return AgentRunResult(process, tuple(events), completed, error, final_text, tuple(commands), tuple(approvals))


def _summary(event: dict[str, Any]) -> str:
    kind = str(event.get("type", "unknown"))
    if kind == "final":
        reason = event.get("reason")
        reason_kind = reason.get("kind") if isinstance(reason, dict) else "missing"
        return f"DeepSeek final reason={reason_kind}"
    if kind == "tool_result":
        status = event.get("status", "unknown")
        return f"DeepSeek tool result status={status}"
    if kind in {"session", "status", "error"}:
        return f"DeepSeek {kind}"
    return f"DeepSeek {kind} event"


def _failure(process: ProcessResult, reason: str | None, has_events: bool) -> str:
    if process.cancelled:
        return "cancelled"
    if process.timed_out:
        return "timeout"
    if process.truncated:
        return "log_capacity_exceeded"
    if not has_events:
        return "missing_protocol_events"
    if reason is None:
        return "missing_final_or_terminal_reason"
    if reason != "completed":
        return f"terminal_reason:{reason}"
    return f"nonzero_exit:{process.returncode}"

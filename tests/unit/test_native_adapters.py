from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_center.adapters.base import AgentRequest
from agent_center.adapters.codex import CodexExecAdapter
from agent_center.adapters.deepseek import DeepSeekHeadlessAdapter
from agent_center.config import AppConfiguration
from agent_center.runtime.process import ProcessResult
from agent_center.verification import VerificationService


@pytest.fixture
def agent_request(verified_config: AppConfiguration, tmp_path: Path) -> AgentRequest:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    return AgentRequest(
        "Verify",
        "task",
        "run",
        1,
        workspace,
        verified_config.profiles["codex"],
        verified_config.agents["codex"],
        "saved plan",
        10,
        logs,
    )


def test_codex_native_flags_and_safe_modes(agent_request: AgentRequest) -> None:
    for mode in ("read-only", "workspace-write"):
        launch = CodexExecAdapter().build_launch(
            replace(agent_request, profile=agent_request.profile.model_copy(update={"permission_mode": mode}))
        )
        assert launch.argv[:3] == ("--ask-for-approval", "never", "exec")
        assert launch.argv[launch.argv.index("--sandbox") + 1] == mode
        assert "--json" in launch.argv and "--ignore-user-config" in launch.argv
        assert not any(flag in launch.argv for flag in ("--yolo", "--approve-for-me", "danger-full-access"))
        assert "AGENT_CENTER_API_KEY" not in launch.environment


def test_deepseek_isolated_overlay_and_no_session_replay(
    agent_request: AgentRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = agent_request.log_dir / "dsh"
    (install / "lib").mkdir(parents=True)
    (install / "lib/bin.js").write_text("// fake")
    profile = agent_request.profile.model_copy(
        update={"agent_id": "deepseek", "provider": "explicit-route", "credential_env": ["DEEPSEEK_API_KEY"]}
    )
    config = agent_request.config.model_copy(update={"adapter": "deepseek_headless", "cli_install_dir": str(install)})
    monkeypatch.setenv("AGENT_CENTER_API_KEY", "supervisor-secret")
    monkeypatch.setenv("GUI_TOKEN", "gui-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "agent-key")
    for mode in ("read-only", "workspace-write"):
        launch = DeepSeekHeadlessAdapter().build_launch(
            replace(agent_request, config=config, profile=profile.model_copy(update={"permission_mode": mode}))
        )
        assert launch.argv[3:5] == ("--profile", "headless")
        assert launch.argv[launch.argv.index("--permission-mode") + 1] == mode
        assert launch.argv[launch.argv.index("--approval-policy") + 1] == "never"
        assert "--session-id" not in launch.argv and "--add-dir" not in launch.argv
        assert launch.environment["DSH_HOME"].startswith(str(agent_request.log_dir))
        assert "AGENT_CENTER_API_KEY" not in launch.environment and "GUI_TOKEN" not in launch.environment
        overlay = json.loads((agent_request.log_dir / "runtime-overlay.yaml").read_text())
        assert overlay[0]["config"]["provider"] == "explicit-route"


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "no_cwd",
        "bad_exit",
        "missing_terminal",
        "truncated",
        "final_text",
        "cancelled",
        "timeout",
        "duplicate",
        "process_exit",
    ],
)
def test_codex_jsonl_evidence(agent_request: AgentRequest, fault: str) -> None:
    command = ("python", "-B", "-m", "pytest", "-p", "no:cacheprovider", "-q", "tests/test_ok.py")
    item = {
        "id": "cmd",
        "type": "command_execution",
        "command": " ".join(command),
        "cwd": str(agent_request.workspace),
        "status": "completed",
        "exit_code": 0,
        "aggregated_output": "passed",
    }
    if fault == "no_cwd":
        item.pop("cwd")
    if fault == "bad_exit":
        item["exit_code"] = 1
    events = [{"type": "item.completed", "item": item}, {"type": "turn.completed"}]
    if fault == "missing_terminal":
        events.pop()
    if fault == "duplicate":
        events.insert(1, events[0])
    if fault == "final_text":
        events[0] = {"type": "item.completed", "item": {"type": "agent_message", "text": "pytest PASSED, exit=0"}}
    data = "".join(json.dumps(e) + "\n" for e in events)
    if fault == "truncated":
        data = data[:-1]
    (agent_request.log_dir / "stdout.log").write_text(data)
    process = ProcessResult(
        1 if fault == "process_exit" else 0, len(data), 0, False, fault == "timeout", fault == "cancelled"
    )
    parsed = CodexExecAdapter().parse(agent_request, process)
    status = VerificationService.command_status(parsed, command, agent_request.workspace)
    assert status == ("PASSED" if fault == "none" else "FAILED" if fault == "bad_exit" else "UNVERIFIED")


@pytest.mark.parametrize(
    "status,text",
    [("completed", "all passed\n[exit code: 0]"), ("completed", "failed\n[exit code: 1]"), ("error", "denied")],
)
def test_deepseek_rendered_result_is_not_structured_exit(agent_request: AgentRequest, status: str, text: str) -> None:
    events = [
        {"type": "session", "sessionId": "s", "cwd": str(agent_request.workspace)},
        {
            "type": "tool_call",
            "callId": "c",
            "tool": "bash",
            "input": {"command": "python -m pytest", "workdir": str(agent_request.workspace)},
        },
        {"type": "tool_result", "callId": "c", "status": status, "result": text},
        {"type": "final", "sessionId": "s", "reason": {"kind": "completed"}, "text": "passed"},
    ]
    (agent_request.log_dir / "stdout.log").write_text("".join(json.dumps(e) + "\n" for e in events))
    parsed = DeepSeekHeadlessAdapter().parse(agent_request, ProcessResult(0, 1, 0, False, False, False))
    assert len(parsed.commands) == 1 and parsed.commands[0].exit_code is None
    status = VerificationService.command_status(parsed, ("python", "-m", "pytest"), agent_request.workspace)
    assert status == "UNVERIFIED"


@pytest.mark.parametrize("fault", ["wrong_session", "orphan", "unfinished_call", "final_failed", "no_final"])
def test_deepseek_protocol_fail_closed(agent_request: AgentRequest, fault: str) -> None:
    events = [{"type": "session", "sessionId": "s"}]
    if fault == "orphan":
        events.append({"type": "tool_result", "callId": "unknown", "status": "completed"})
    if fault == "unfinished_call":
        events.append({"type": "tool_call", "callId": "unfinished", "tool": "bash", "input": {"command": "true"}})
    if fault != "no_final":
        events.append(
            {
                "type": "final",
                "sessionId": "other" if fault == "wrong_session" else "s",
                "reason": {"kind": "failed" if fault == "final_failed" else "completed"},
            }
        )
    (agent_request.log_dir / "stdout.log").write_text("".join(json.dumps(e) + "\n" for e in events))
    assert not DeepSeekHeadlessAdapter().parse(agent_request, ProcessResult(0, 1, 0, False, False, False)).completed


def test_codex_custom_connection_uses_per_call_provider(
    agent_request: AgentRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_API_KEY", "synthetic-key")
    profile = agent_request.profile.model_copy(
        update={"base_url": "https://gateway.example/v1", "api_key_env": "CODEX_API_KEY"}
    )
    launch = CodexExecAdapter().build_launch(replace(agent_request, profile=profile))
    assert 'model_provider="agent_center"' in launch.argv
    assert any('base_url="https://gateway.example/v1"' in arg for arg in launch.argv)
    assert launch.environment["CODEX_API_KEY"] == "synthetic-key"
    assert all("synthetic-key" not in arg for arg in launch.argv)


def test_codex_subscription_default_needs_no_api_or_model(agent_request: AgentRequest) -> None:
    profile = agent_request.profile.model_copy(update={"model": "", "base_url": "", "api_key_env": ""})
    launch = CodexExecAdapter().build_launch(replace(agent_request, profile=profile))
    assert "--model" not in launch.argv
    assert not any("model_provider=" in arg for arg in launch.argv)
    assert "OPENAI_API_KEY" not in launch.environment
    assert "CODEX_API_KEY" not in launch.environment

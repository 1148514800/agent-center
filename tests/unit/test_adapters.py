from __future__ import annotations

import json
from pathlib import Path

from agent_center.adapters.base import AgentRequest
from agent_center.adapters.deepseek import DeepSeekHeadlessAdapter
from agent_center.config import AgentConfig, ProfileConfig
from agent_center.runtime.process import ProcessResult


def test_deepseek_internal_tool_error_prevents_completed_result(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    request = AgentRequest(
        objective="test",
        task_id="task_1",
        run_id="run_1",
        requirements_version=1,
        workspace=workspace,
        profile=ProfileConfig(
            agent_id="deepseek", provider="deepseek-official", model="dsh-test", permission_mode="read-only"
        ),
        config=AgentConfig(
            adapter="deepseek_headless", executable="/tmp/dsh", description="fake", default_profile="deepseek"
        ),
        context="synthetic",
        timeout_seconds=10,
        log_dir=log_dir,
    )
    events = [
        {"type": "session", "sessionId": "s1"},
        {
            "type": "tool_result",
            "callId": "c1",
            "status": "error",
            "error": {"name": "SandboxDenied", "code": "DENIED", "reason": "blocked"},
        },
        {"type": "final", "sessionId": "s1", "reason": {"kind": "completed"}, "text": "done"},
    ]
    (log_dir / "stdout.log").write_text("\n".join(json.dumps(event) for event in events) + "\n")
    result = ProcessResult(
        returncode=0, stdout_bytes=100, stderr_bytes=0, truncated=False, timed_out=False, cancelled=False
    )
    parsed = DeepSeekHeadlessAdapter().parse(request, result)
    assert not parsed.completed
    assert parsed.error and "internal_tool_failure" in parsed.error

from __future__ import annotations

from pathlib import Path

import pytest

from agent_center.policy.approvals import ApprovalManager
from agent_center.storage.database import Store
from agent_center.storage.redaction import StreamingRedactor, redact_text


def test_known_secret_redacted_across_stream_boundary() -> None:
    marker = "SYNTHETIC_SECRET_DO_NOT_LEAK"
    redactor = StreamingRedactor([marker], overlap=8)
    output = redactor.feed(("prefix " + marker[:15]).encode())
    output += redactor.feed((marker[15:] + " suffix\n").encode())
    output += redactor.feed(b"", final=True)
    assert marker.encode() not in output
    assert b"[REDACTED]" in output


def test_common_key_patterns_redacted() -> None:
    assert "SECRET_VALUE_DO_NOT_LEAK" not in redact_text("api_key=SECRET_VALUE_DO_NOT_LEAK")
    assert "sk-test-verylongcredential" not in redact_text("sk-test-verylongcredential")


def test_approval_scope_is_workspace_bound_and_origin_signed(store: Store, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_id = store.create_task("goal", workspace, {"config": 1})
    manager = ApprovalManager(store, b"k" * 32)
    target = workspace / "subdir"
    approval_id = manager.create(task_id, 1, {"config": 1}, "write directory", target, "risk")
    token = manager.decision_token(approval_id, "approve")
    with pytest.raises(PermissionError):
        manager.decide(approval_id, "approve", "forged", source="agent")
    assert manager.decide(approval_id, "approve", token, source="user")
    assert manager.approved_scopes(task_id, 1, {"config": 1}) == (str(target),)
    assert manager.approved_scopes(task_id, 2, {"config": 1}) == ()
    assert manager.approved_scopes(task_id, 1, {"config": 2}) == ()


def test_approval_outside_workspace_rejected(store: Store, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_id = store.create_task("goal", workspace, {})
    manager = ApprovalManager(store, b"k" * 32)
    with pytest.raises(ValueError, match="under the existing task workspace"):
        manager.create(task_id, 1, {}, "write", tmp_path / "elsewhere", "risk")

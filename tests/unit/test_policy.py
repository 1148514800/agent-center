from __future__ import annotations

from pathlib import Path

import pytest

from agent_center.domain import TaskStatus
from agent_center.policy.approvals import ApprovalManager
from agent_center.storage.database import Store
from agent_center.storage.redaction import StreamingRedactor, redact_text


def _request_approval(
    manager: ApprovalManager, store: Store, task_id: str, workspace: Path, *, ttl_seconds: int = 900
) -> str:
    pending_action = {
        "action": "request_approval",
        "operation": "narrow operation",
        "target_path": str(workspace),
        "risk": "synthetic risk",
        "reason_summary": "synthetic request",
    }
    approval_id = manager.create(
        task_id,
        1,
        {"config": 1},
        "narrow operation",
        workspace,
        "synthetic risk",
        pending_action=pending_action,
        ttl_seconds=ttl_seconds,
    )
    return approval_id


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


def test_approval_is_invalidated_when_requirement_changes_before_decision(store: Store, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_id = store.create_task("Implement a narrow change", workspace, {"config": 1})
    store.set_status(task_id, TaskStatus.PLANNING)
    manager = ApprovalManager(store, b"k" * 32)
    approval_id = _request_approval(manager, store, task_id, workspace)
    token = manager.decision_token(approval_id, "approve")
    store.add_message(task_id, "cancel the operation")
    store.apply_pending_messages(task_id)
    assert not manager.decide(approval_id, "approve", token, source="unit_test")
    assert store.pending_approval(task_id) is None
    status = store._connection.execute("SELECT status FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
    assert status[0] == "expired"


def test_second_pending_approval_is_rejected_atomically(store: Store, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_id = store.create_task("Implement a narrow change", workspace, {"config": 1})
    store.set_status(task_id, TaskStatus.PLANNING)
    manager = ApprovalManager(store, b"k" * 32)
    approval_id = _request_approval(manager, store, task_id, workspace)
    with pytest.raises(ValueError, match="Another approval request is already pending"):
        _request_approval(manager, store, task_id, workspace)
    rows = store._connection.execute(
        "SELECT COUNT(*) FROM approvals WHERE task_id=? AND status='pending'", (task_id,)
    ).fetchone()
    assert rows[0] == 1
    assert store.pending_approval(task_id)[0] == approval_id


def test_approval_decision_transitions_waiting_task_atomically(store: Store, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_id = store.create_task("Implement a narrow change", workspace, {"config": 1})
    store.set_status(task_id, TaskStatus.PLANNING)
    manager = ApprovalManager(store, b"k" * 32)
    approval_id = _request_approval(manager, store, task_id, workspace)
    token = manager.decision_token(approval_id, "approve")
    assert manager.decide(approval_id, "approve", token, source="unit_test")
    assert store.task(task_id)["status"] == TaskStatus.PLANNING
    assert store.pending_approval(task_id) is not None
    assert manager.approved_scopes(task_id, 1, {"config": 1}) == (str(workspace),)


def test_denied_approval_never_leaves_a_resumable_pending_action(store: Store, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_id = store.create_task("Implement a narrow change", workspace, {"config": 1})
    store.set_status(task_id, TaskStatus.PLANNING)
    manager = ApprovalManager(store, b"k" * 32)
    approval_id = _request_approval(manager, store, task_id, workspace)
    token = manager.decision_token(approval_id, "deny")
    assert not manager.decide(approval_id, "deny", token, source="unit_test")
    assert store.task(task_id)["status"] == TaskStatus.BLOCKED
    assert store.pending_approval(task_id) is None


def test_approval_requires_matching_version_snapshot_and_absolute_target(store: Store, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_id = store.create_task("goal", workspace, {"config": 1})
    store.set_status(task_id, TaskStatus.PLANNING)
    manager = ApprovalManager(store, b"k" * 32)
    pending_action = {
        "action": "request_approval",
        "operation": "write",
        "target_path": str(workspace),
        "risk": "synthetic",
        "reason_summary": "test",
    }
    with pytest.raises(ValueError, match="stale"):
        manager.create(task_id, 2, {"config": 1}, "write", workspace, "risk", pending_action=pending_action)
    with pytest.raises(ValueError, match="snapshot"):
        manager.create(task_id, 1, {"config": 2}, "write", workspace, "risk", pending_action=pending_action)
    with pytest.raises(ValueError, match="absolute"):
        manager.create(task_id, 1, {"config": 1}, "write", Path("relative"), "risk", pending_action=pending_action)
    with pytest.raises(ValueError, match="under the existing task workspace"):
        manager.create(
            task_id, 1, {"config": 1}, "write", tmp_path / "elsewhere", "risk", pending_action=pending_action
        )


def test_expired_approval_clears_pending_task_marker(store: Store, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task_id = store.create_task("goal", workspace, {"config": 1})
    store.set_status(task_id, TaskStatus.PLANNING)
    manager = ApprovalManager(store, b"k" * 32)
    approval_id = _request_approval(manager, store, task_id, workspace, ttl_seconds=1)
    store._connection.execute("UPDATE approvals SET expires_at='0' WHERE approval_id=?", (approval_id,))
    assert not manager.decide(
        approval_id, "approve", manager.decision_token(approval_id, "approve"), source="unit_test"
    )
    assert store.pending_approval(task_id) is None
    status = store._connection.execute("SELECT status FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
    assert status[0] == "expired"

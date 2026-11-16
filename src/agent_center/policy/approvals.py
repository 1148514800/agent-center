from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from agent_center.storage.database import Store, canonical, new_id, now
from agent_center.storage.redaction import redact_text, redact_value


class ApprovalManager:
    """Auditable one-shot decisions that never manufacture backend permissions."""

    def __init__(self, store: Store, secret_key: bytes):
        if len(secret_key) < 32:
            raise ValueError("Approval signing key must contain at least 32 bytes")
        self.store = store
        self.secret_key = secret_key

    @staticmethod
    def config_digest(config_snapshot: dict[str, Any]) -> str:
        return hashlib.sha256(canonical(config_snapshot).encode()).hexdigest()

    def create(
        self,
        task_id: str,
        requirements_version: int,
        config_snapshot: dict[str, Any],
        operation: str,
        target_path: Path,
        risk: str,
        *,
        pending_action: dict[str, Any],
        ttl_seconds: int = 900,
    ) -> str:
        task = self.store.task(task_id)
        if task is None:
            raise KeyError(task_id)
        workspace = Path(task["workspace"]).resolve(strict=True)
        if not target_path.is_absolute():
            raise ValueError("Approval target must be an explicit absolute directory path")
        target = target_path.resolve(strict=False)
        try:
            target.relative_to(workspace)
        except ValueError as exc:
            raise ValueError("Approval target must be under the existing task workspace") from exc
        if not operation.strip() or not risk.strip() or ttl_seconds <= 0 or ttl_seconds > 86400:
            raise ValueError("Invalid approval operation, risk, or expiry")
        approval_id = new_id("apr")
        digest = self.config_digest(config_snapshot)
        expires_at = time.time() + ttl_seconds
        with self.store.transaction() as conn:
            task_row = conn.execute(
                "SELECT requirements_version,status,config_snapshot,pending_approval_id FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            pending_message = conn.execute(
                "SELECT 1 FROM messages WHERE task_id=? AND applied_at IS NULL LIMIT 1", (task_id,)
            ).fetchone()
            if task_row is None:
                raise KeyError(task_id)
            if task_row[0] != requirements_version or pending_message:
                raise ValueError("Approval scope is stale because requirements changed or are pending")
            if task_row[3] is not None:
                raise ValueError("Another approval request is already pending for this task")
            if task_row[1] not in {"planning", "running", "waiting_approval"}:
                raise ValueError("Task status does not permit a new approval request")
            if self.config_digest(json.loads(task_row[2])) != digest:
                raise ValueError("Approval configuration snapshot does not match the task snapshot")
            conn.execute(
                "INSERT INTO approvals(approval_id,task_id,requirements_version,config_hash,operation,target_path,"
                "risk,status,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    approval_id,
                    task_id,
                    requirements_version,
                    digest,
                    operation[:500],
                    str(target),
                    risk[:1000],
                    "pending",
                    now(),
                    str(expires_at),
                ),
            )
            conn.execute(
                "UPDATE tasks SET pending_approval_id=?,pending_action_json=?,status=?,"
                "state_version=state_version+1,updated_at=? WHERE task_id=? "
                "AND requirements_version=? AND pending_approval_id IS NULL",
                (
                    approval_id,
                    redact_text(canonical(redact_value(pending_action))),
                    "waiting_approval",
                    now(),
                    task_id,
                    requirements_version,
                ),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise ValueError("Approval target task changed while saving request")
            self.store.emit(
                conn,
                task_id,
                "approval_requested",
                {
                    "approval_id": approval_id,
                    "requirements_version": requirements_version,
                    "operation": operation[:500],
                    "target_path": str(target),
                    "risk": risk[:1000],
                    "expires_at_epoch": expires_at,
                },
            )
        return approval_id

    def decision_token(self, approval_id: str, decision: str) -> str:
        if decision not in {"approve", "deny"}:
            raise ValueError("Decision must be approve or deny")
        message = f"{approval_id}:{decision}".encode()
        return hmac.new(self.secret_key, message, hashlib.sha256).hexdigest()

    def decide(self, approval_id: str, decision: str, token: str, *, source: str) -> bool:
        expected = self.decision_token(approval_id, decision)
        if not hmac.compare_digest(expected, token):
            raise PermissionError("Approval decision did not originate from the trusted local interface")
        self.store.expire_approvals()
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT task_id,requirements_version,config_hash,target_path,status,expires_at "
                "FROM approvals WHERE approval_id=?",
                (approval_id,),
            ).fetchone()
            if row is None or row[4] != "pending":
                return False
            if float(row[5]) <= time.time():
                conn.execute(
                    "UPDATE approvals SET status='expired',decision_at=?,decision_source='expired' WHERE approval_id=?",
                    (now(), approval_id),
                )
                conn.execute(
                    "UPDATE tasks SET pending_approval_id=NULL,status=?,state_version=state_version+1,updated_at=? "
                    "WHERE task_id=? AND pending_approval_id=?",
                    ("planning", now(), row[0], approval_id),
                )
                self.store.emit(conn, row[0], "approval_expired", {"approval_id": approval_id})
                return False
            task = conn.execute(
                "SELECT requirements_version,config_snapshot,pending_approval_id FROM tasks WHERE task_id=?",
                (row[0],),
            ).fetchone()
            pending_message = conn.execute(
                "SELECT 1 FROM messages WHERE task_id=? AND applied_at IS NULL LIMIT 1",
                (row[0],),
            ).fetchone()
            stale = (
                task is None
                or task[0] != row[1]
                or task[2] != approval_id
                or pending_message is not None
                or self.config_digest(json.loads(task[1]) if task else {}) != row[2]
            )
            if stale:
                conn.execute(
                    "UPDATE approvals SET status='expired',decision_at=?,decision_source='scope_changed' "
                    "WHERE approval_id=?",
                    (now(), approval_id),
                )
                if task is not None and task[2] == approval_id:
                    conn.execute(
                        "UPDATE tasks SET pending_approval_id=NULL,pending_action_json=NULL,status=?,"
                        "state_version=state_version+1,updated_at=? WHERE task_id=?",
                        ("blocked", now(), row[0]),
                    )
                self.store.emit(conn, row[0], "approval_expired", {"approval_id": approval_id})
                return False
            status = "approved" if decision == "approve" else "denied"
            conn.execute(
                "UPDATE approvals SET status=?,decision_at=?,decision_source=? "
                "WHERE approval_id=? AND status='pending'",
                (status, now(), source, approval_id),
            )
            task_status = "planning" if decision == "approve" else "blocked"
            changed = conn.execute(
                "UPDATE tasks SET status=?,pending_approval_id=CASE WHEN ?='denied' THEN NULL "
                "ELSE pending_approval_id END,"
                "pending_action_json=CASE WHEN ?='denied' THEN NULL ELSE pending_action_json END,"
                "state_version=state_version+1,updated_at=? "
                "WHERE task_id=? AND status='waiting_approval' AND pending_approval_id=?",
                (task_status, status, status, now(), row[0], approval_id),
            ).rowcount
            if changed != 1:
                conn.execute(
                    "UPDATE approvals SET status='expired',decision_at=?,decision_source='task_state_changed' "
                    "WHERE approval_id=?",
                    (now(), approval_id),
                )
                self.store.emit(conn, row[0], "approval_expired", {"approval_id": approval_id})
                return False
            self.store.emit(
                conn,
                row[0],
                f"approval_{status}",
                {
                    "approval_id": approval_id,
                    "source": source,
                    "target_path": row[3],
                    "requirements_version": row[1],
                },
            )
            return decision == "approve"

    def approved_scopes(
        self, task_id: str, requirements_version: int, config_snapshot: dict[str, Any]
    ) -> tuple[str, ...]:
        self.store.expire_approvals()
        digest = self.config_digest(config_snapshot)
        rows = self.store._connection.execute(
            "SELECT target_path FROM approvals WHERE task_id=? AND requirements_version=? "
            "AND config_hash=? AND status='approved' AND CAST(expires_at AS REAL)>?",
            (task_id, requirements_version, digest, time.time()),
        ).fetchall()
        task = self.store.task(task_id)
        if not task:
            return ()
        workspace = Path(task["workspace"]).resolve(strict=True)
        scopes: list[str] = []
        for row in rows:
            target = Path(row[0]).resolve(strict=False)
            try:
                target.relative_to(workspace)
            except ValueError:
                continue
            scopes.append(str(target))
        return tuple(scopes)

    def expire_by_scope_change(self, task_id: str) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE approvals SET status='expired' WHERE task_id=? AND status='approved'",
                (task_id,),
            )
            self.store.emit(conn, task_id, "approvals_invalidated", {"reason": "scope_changed"})


def create_signing_key(data_dir: Path) -> bytes:
    path = data_dir / "approval.key"
    if path.exists():
        key = path.read_bytes()
        if len(key) != 32:
            raise ValueError("Local approval key is malformed")
        return key
    key = secrets.token_bytes(32)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(key)
    return key

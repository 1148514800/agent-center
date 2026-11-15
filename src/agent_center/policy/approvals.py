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


class ApprovalManager:
    """One-shot authorization bound to task requirements, config, and an exact workspace path."""

    def __init__(self, store: Store, secret_key: bytes):
        if len(secret_key) < 32:
            raise ValueError("Approval signing key must contain at least 32 bytes")
        self.store = store
        self.secret_key = secret_key

    def config_digest(self, config_snapshot: dict[str, Any]) -> str:
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
        ttl_seconds: int = 900,
    ) -> str:
        task = self.store.task(task_id)
        if task is None:
            raise KeyError(task_id)
        workspace = Path(task["workspace"]).resolve(strict=True)
        if not target_path.is_absolute():
            raise ValueError("Approval target must be an explicit absolute directory path")
        target = target_path.expanduser().resolve(strict=False)
        # Scope is directory-level, never a claimed single-command grant.
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
                "SELECT requirements_version,status,config_snapshot FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            pending_message = conn.execute(
                "SELECT 1 FROM messages WHERE task_id=? AND applied_at IS NULL LIMIT 1",
                (task_id,),
            ).fetchone()
            if task_row is None:
                raise KeyError(task_id)
            if task_row[0] != requirements_version or pending_message:
                raise ValueError("Approval scope is stale because requirements changed or are pending")
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
        """Only a trusted local CLI/UI caller can access the signing key and produce this token."""
        expected = self.decision_token(approval_id, decision)
        if not hmac.compare_digest(expected, token):
            raise PermissionError("Approval decision did not originate from the trusted user interface")
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT task_id,requirements_version,config_hash,target_path,status,expires_at "
                "FROM approvals WHERE approval_id=?",
                (approval_id,),
            ).fetchone()
            if row is None or row[4] != "pending":
                return False
            if float(row[5]) < time.time():
                conn.execute(
                    "UPDATE approvals SET status='expired',decision_at=?,decision_source='expired' WHERE approval_id=?",
                    (now(), approval_id),
                )
                self.store.emit(conn, row[0], "approval_expired", {"approval_id": approval_id})
                return False
            task = conn.execute(
                "SELECT requirements_version,config_snapshot FROM tasks WHERE task_id=?", (row[0],)
            ).fetchone()
            config_changed = (
                task is None
                or task[0] != row[1]
                or self.config_digest(json.loads(task[1]) if task is not None else {}) != row[2]
            )
            if config_changed:
                conn.execute(
                    "UPDATE approvals SET status='expired',decision_at=?,decision_source='scope_changed' "
                    "WHERE approval_id=?",
                    (now(), approval_id),
                )
                self.store.emit(conn, row[0], "approval_expired", {"approval_id": approval_id})
                return False
            status = "approved" if decision == "approve" else "denied"
            conn.execute(
                "UPDATE approvals SET status=?,decision_at=?,decision_source=? WHERE approval_id=?",
                (status, now(), source, approval_id),
            )
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
            self.store.emit(conn, task_id, "approvals_invalidated", {"reason": "requirements_or_config_changed"})


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

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import sqlite3
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from agent_center.domain import TaskStatus
from agent_center.storage.redaction import redact_text, redact_value

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(
 task_id TEXT PRIMARY KEY, goal TEXT NOT NULL, workspace TEXT NOT NULL, status TEXT NOT NULL,
 requirements_version INTEGER NOT NULL DEFAULT 1, state_version INTEGER NOT NULL DEFAULT 1,
 config_snapshot TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 agent_calls INTEGER NOT NULL DEFAULT 0, supervisor_calls INTEGER NOT NULL DEFAULT 0,
 active_seconds REAL NOT NULL DEFAULT 0, active_started_at REAL, plan_json TEXT NOT NULL DEFAULT '[]',
 last_error TEXT, criteria_json TEXT NOT NULL DEFAULT '[]',
 pending_approval_id TEXT, pending_action_json TEXT
);
CREATE TABLE IF NOT EXISTS messages(
 message_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id), sequence INTEGER NOT NULL,
 text TEXT NOT NULL, intent TEXT NOT NULL DEFAULT 'requirement', received_at TEXT NOT NULL,
 applied_at TEXT, applied_version INTEGER
);
CREATE TABLE IF NOT EXISTS runs(
 run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id), agent_id TEXT NOT NULL,
 status TEXT NOT NULL, requirements_version INTEGER NOT NULL, config_snapshot TEXT NOT NULL,
 profile_snapshot TEXT NOT NULL, objective TEXT NOT NULL, started_at TEXT, ended_at TEXT,
 pid INTEGER, process_start_identity TEXT, process_command_line TEXT, executable_path TEXT,
 exit_code INTEGER, error TEXT, log_path TEXT NOT NULL,
 event_complete INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events(
 event_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id), run_id TEXT,
 sequence INTEGER NOT NULL, type TEXT NOT NULL, timestamp TEXT NOT NULL,
 payload TEXT NOT NULL, schema_version INTEGER NOT NULL DEFAULT 1, UNIQUE(task_id, sequence)
);
CREATE TABLE IF NOT EXISTS approvals(
 approval_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id), run_id TEXT,
 requirements_version INTEGER NOT NULL, config_hash TEXT NOT NULL, operation TEXT NOT NULL,
 target_path TEXT NOT NULL, risk TEXT NOT NULL, status TEXT NOT NULL,
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL, decision_at TEXT, decision_source TEXT
);
CREATE TABLE IF NOT EXISTS evidence(
 evidence_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id), run_id TEXT,
 requirements_version INTEGER NOT NULL, source TEXT NOT NULL, summary TEXT NOT NULL,
 path TEXT, sha256 TEXT, workspace_sha256 TEXT, complete INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_criteria(
 evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id), criterion_id TEXT NOT NULL,
 PRIMARY KEY(evidence_id,criterion_id)
);
CREATE TABLE IF NOT EXISTS conversations(
 conversation_id TEXT PRIMARY KEY, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages(
 message_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
 sequence INTEGER NOT NULL, text TEXT NOT NULL, answer TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'pending', reason TEXT NOT NULL DEFAULT '', workspace TEXT NOT NULL DEFAULT '',
 task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL, created_at TEXT NOT NULL,
 UNIQUE(conversation_id,sequence)
);
CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_task_seq ON events(task_id, sequence);
CREATE INDEX IF NOT EXISTS idx_messages_inbox ON messages(task_id, applied_at, sequence);
"""


def now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class WorkspaceLock:
    def __init__(self, path: Path):
        self.path = path
        self._stream: Any = None

    def __enter__(self) -> WorkspaceLock:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        self._stream = self.path.open("a+")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._stream.close()
            raise RuntimeError("Another Agent Center process holds the data-directory lock") from exc
        self._stream.write(f"pid={os.getpid()}\n")
        self._stream.flush()
        return self

    def __exit__(self, *_: object) -> None:
        if self._stream is not None:
            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
            self._stream.close()


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir.expanduser().resolve()
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.data_dir, 0o700)
        self.db_path = self.data_dir / "state.sqlite3"
        self._connection = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        os.chmod(self.db_path, 0o600)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=10000")
        self._connection.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        self._connection.execute("INSERT OR IGNORE INTO schema_migrations VALUES(7, ?)", (now(),))
        columns = {row[1] for row in self._connection.execute("PRAGMA table_info(tasks)")}
        if "execution_required" in columns:
            self._connection.execute("ALTER TABLE tasks DROP COLUMN execution_required")
        self._connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(6, ?)", (now(),))
        if "active_started_at" not in columns:
            self._connection.execute("ALTER TABLE tasks ADD COLUMN active_started_at REAL")
        if "pending_action_json" not in columns:
            self._connection.execute("ALTER TABLE tasks ADD COLUMN pending_action_json TEXT")
        if "pending_approval_id" not in columns:
            self._connection.execute("ALTER TABLE tasks ADD COLUMN pending_approval_id TEXT")
        evidence_columns = {row[1] for row in self._connection.execute("PRAGMA table_info(evidence)")}
        if "workspace_sha256" not in evidence_columns:
            self._connection.execute("ALTER TABLE evidence ADD COLUMN workspace_sha256 TEXT")
        for name in ("plan_sha256", "capability_identity", "verification_status"):
            if name not in evidence_columns:
                self._connection.execute(f"ALTER TABLE evidence ADD COLUMN {name} TEXT")
        self._connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(5, ?)", (now(),))
        run_columns = {row[1] for row in self._connection.execute("PRAGMA table_info(runs)")}
        if "process_command_line" not in run_columns:
            self._connection.execute("ALTER TABLE runs ADD COLUMN process_command_line TEXT")
        if "executable_path" not in run_columns:
            self._connection.execute("ALTER TABLE runs ADD COLUMN executable_path TEXT")
        self._connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, ?)", (now(),))
        self._connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(2, ?)", (now(),))
        self._connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(3, ?)", (now(),))
        self._connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(4, ?)", (now(),))

    def close(self) -> None:
        self._connection.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield self._connection
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def emit(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        event_type: str,
        payload: dict[str, Any],
        run_id: str | None = None,
    ) -> None:
        sequence = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0)+1 FROM events WHERE task_id=?", (task_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO events(event_id,task_id,run_id,sequence,type,timestamp,payload) VALUES(?,?,?,?,?,?,?)",
            (
                new_id("evt"),
                task_id,
                run_id,
                sequence,
                event_type,
                now(),
                redact_text(canonical(redact_value(payload))),
            ),
        )

    def create_conversation(self) -> str:
        conversation_id = new_id("chat")
        self._connection.execute("INSERT INTO conversations VALUES(?,?)", (conversation_id, now()))
        return conversation_id

    def conversation(self, conversation_id: str) -> dict[str, Any]:
        if not self._connection.execute(
            "SELECT 1 FROM conversations WHERE conversation_id=?", (conversation_id,)
        ).fetchone():
            raise ValueError("会话不存在")
        messages = [dict(row) for row in self._connection.execute(
            "SELECT * FROM chat_messages WHERE conversation_id=? ORDER BY sequence", (conversation_id,))]
        return {"conversation_id": conversation_id, "messages": messages}

    def list_conversations(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._connection.execute(
            "SELECT c.conversation_id, COALESCE((SELECT text FROM chat_messages m "
            "WHERE m.conversation_id=c.conversation_id ORDER BY sequence LIMIT 1),'新会话') AS title "
            "FROM conversations c ORDER BY c.rowid DESC LIMIT 100")]

    def add_chat_message(self, conversation_id: str, text: str, workspace: str) -> str:
        self.conversation(conversation_id)
        message_id = new_id("chatmsg")
        with self.transaction() as conn:
            sequence = conn.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM chat_messages WHERE conversation_id=?",
                (conversation_id,)).fetchone()[0]
            conn.execute(
                "INSERT INTO chat_messages(message_id,conversation_id,sequence,text,workspace,created_at) "
                "VALUES(?,?,?,?,?,?)", (message_id, conversation_id, sequence, redact_text(text), workspace, now()))
        return message_id

    def update_chat_message(self, message_id: str, *, status: str, answer: str = "", reason: str = "",
                            task_id: str | None = None) -> None:
        self._connection.execute(
            "UPDATE chat_messages SET status=?,answer=?,reason=?,task_id=? WHERE message_id=?",
            (status, redact_text(answer), redact_text(reason), task_id, message_id))

    def delete_task(self, task_id: str) -> None:
        """Delete internal rows only. Workspace files and CLI logs are never followed/deleted."""
        with self.transaction() as conn:
            task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if task is None:
                raise ValueError("任务不存在")
            unsafe = conn.execute(
                "SELECT 1 FROM runs WHERE task_id=? AND (status NOT IN ('completed','reconciled','failed') "
                "OR (status IN ('completed','failed') AND event_complete=0))", (task_id,)).fetchone()
            if task["active_started_at"] is not None or task["status"] in {"running", "verifying"} or unsafe:
                raise ValueError("请先停止 / 取消运行并对账，再删除任务")
            conn.execute("DELETE FROM evidence_criteria WHERE evidence_id IN "
                         "(SELECT evidence_id FROM evidence WHERE task_id=?)", (task_id,))
            for table in ("evidence", "approvals", "events", "runs", "messages"):
                conn.execute(f"DELETE FROM {table} WHERE task_id=?", (task_id,))
            conn.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))

    def create_task(self, goal: str, workspace: Path, snapshot: dict[str, Any]) -> str:
        goal = redact_text(goal)
        task_id = new_id("task")
        stamp = now()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO tasks(task_id,goal,workspace,status,config_snapshot,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    task_id,
                    goal,
                    str(workspace),
                    TaskStatus.CREATED,
                    canonical(snapshot),
                    stamp,
                    stamp,
                ),
            )
            conn.execute(
                "INSERT INTO messages(message_id,task_id,sequence,text,intent,received_at,applied_at,applied_version) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (new_id("msg"), task_id, 1, goal, "initial_goal", stamp, stamp, 1),
            )
            self.emit(conn, task_id, "task_created", {"requirements_version": 1})
        return task_id

    def add_message(self, task_id: str, text: str, intent: str = "requirement") -> str:
        if not text.strip():
            raise ValueError("User message cannot be empty")
        text = redact_text(text)
        message_id = new_id("msg")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM messages WHERE task_id=?", (task_id,)
            ).fetchone()
            task = conn.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(task_id)
            if task[0] in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
                raise RuntimeError("Cannot append requirements to a terminal task; create a new task instead")
            conn.execute(
                "INSERT INTO messages(message_id,task_id,sequence,text,intent,received_at) VALUES(?,?,?,?,?,?)",
                (message_id, task_id, row[0], text, intent, now()),
            )
            self.emit(conn, task_id, "message_received", {"message_id": message_id, "intent": intent})
        return message_id

    def apply_pending_messages(self, task_id: str) -> int:
        with self.transaction() as conn:
            task = conn.execute("SELECT requirements_version FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            pending = conn.execute(
                "SELECT message_id FROM messages WHERE task_id=? AND applied_at IS NULL ORDER BY sequence",
                (task_id,),
            ).fetchall()
            if not pending:
                return int(task[0])
            version = task[0] + 1
            stamp = now()
            for message in pending:
                conn.execute(
                    "UPDATE messages SET applied_at=?, applied_version=? WHERE message_id=?",
                    (stamp, version, message[0]),
                )
            conn.execute(
                "UPDATE tasks SET requirements_version=?,state_version=state_version+1,updated_at=?,"
                "plan_json='[]',criteria_json='[]' "
                "WHERE task_id=?",
                (version, stamp, task_id),
            )
            changed_approvals = conn.execute(
                "SELECT approval_id FROM approvals WHERE task_id=? AND status IN ('pending','approved')",
                (task_id,),
            ).fetchall()
            conn.execute(
                "UPDATE approvals SET status='expired',decision_at=?,decision_source='requirements_changed' "
                "WHERE task_id=? AND status IN ('pending','approved')",
                (stamp, task_id),
            )
            conn.execute(
                "UPDATE tasks SET pending_approval_id=NULL,pending_action_json=NULL "
                "WHERE task_id=? AND pending_approval_id IS NOT NULL",
                (task_id,),
            )
            for approval_row in changed_approvals:
                self.emit(
                    conn,
                    task_id,
                    "approval_expired",
                    {"approval_id": approval_row[0], "reason": "requirements_changed"},
                )
            self.emit(
                conn,
                task_id,
                "requirements_updated",
                {"requirements_version": version, "message_ids": [item[0] for item in pending]},
            )
            return int(version)

    def set_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        error: str | None = None,
        pending_approval_id: str | None = None,
        pending_action: dict[str, Any] | None = None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE tasks SET status=?,last_error=?,pending_approval_id=COALESCE(?,pending_approval_id),"
                "pending_action_json=COALESCE(?,pending_action_json),state_version=state_version+1,updated_at=? "
                "WHERE task_id=?",
                (
                    status,
                    error,
                    pending_approval_id,
                    redact_text(canonical(redact_value(pending_action))) if pending_action else None,
                    now(),
                    task_id,
                ),
            )
            self.emit(conn, task_id, "task_status", {"status": status.value, "error": error})

    def pending_approval(self, task_id: str) -> tuple[str, dict[str, Any]] | None:
        row = self._connection.execute(
            "SELECT pending_approval_id,pending_action_json FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if row is None or row[0] is None or row[1] is None:
            return None
        return str(row[0]), json.loads(row[1])

    def expire_approvals_for_task(self, task_id: str, reason: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE approvals SET status='expired',decision_at=?,decision_source=? "
                "WHERE task_id=? AND status IN ('pending','approved')",
                (now(), reason, task_id),
            )
            self.emit(conn, task_id, "approvals_invalidated", {"reason": reason})

    def clear_pending_approval(self, task_id: str, approval_id: str) -> None:
        with self.transaction() as conn:
            row = conn.execute("SELECT pending_approval_id,status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row[0] != approval_id:
                raise RuntimeError("Approval does not match the task's pending request")
            conn.execute(
                "UPDATE tasks SET pending_approval_id=NULL,pending_action_json=NULL,"
                "state_version=state_version+1,updated_at=? "
                "WHERE task_id=? AND pending_approval_id=?",
                (now(), task_id, approval_id),
            )
            self.emit(conn, task_id, "pending_approval_cleared", {"approval_id": approval_id})

    def record_call(self, task_id: str, kind: str, limit: int) -> int:
        column = "agent_calls" if kind == "agent" else "supervisor_calls"
        with self.transaction() as conn:
            row = conn.execute(f"SELECT {column} FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row[0] >= limit:
                raise RuntimeError(f"{kind} call budget exhausted ({row[0]}/{limit})")
            count = row[0] + 1
            conn.execute(f"UPDATE tasks SET {column}=?,updated_at=? WHERE task_id=?", (count, now(), task_id))
            self.emit(conn, task_id, f"{kind}_call_reserved", {"count": count, "limit": limit})
            return int(count)

    def create_run(
        self,
        task_id: str,
        agent_id: str,
        objective: str,
        profile: dict[str, Any],
        log_path: Path,
        config_snapshot: dict[str, Any],
        expected_version: int,
    ) -> str:
        run_id = new_id("run")
        with self.transaction() as conn:
            task = conn.execute(
                "SELECT requirements_version,status,criteria_json FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if task is None:
                raise KeyError(task_id)
            pending = conn.execute(
                "SELECT 1 FROM messages WHERE task_id=? AND applied_at IS NULL LIMIT 1", (task_id,)
            ).fetchone()
            if task[0] != expected_version or pending:
                raise RuntimeError("Requirements changed or a message is pending; re-plan before creating a run")
            active = conn.execute(
                "SELECT 1 FROM runs WHERE task_id=? AND status IN ('prepared','starting','running','unknown')",
                (task_id,),
            ).fetchone()
            if active:
                raise RuntimeError("An unresolved run exists; reconcile it before starting another")
            if task[1] not in {TaskStatus.PLANNING, TaskStatus.CREATED, TaskStatus.VERIFYING}:
                raise RuntimeError(f"Task status {task[1]} does not permit a new run; reconcile or re-plan first")
            if not json.loads(task[2]):
                raise RuntimeError("Execution task is missing a saved plan and verifiable criteria")
            conn.execute(
                "INSERT INTO runs(run_id,task_id,agent_id,status,requirements_version,config_snapshot,"
                "profile_snapshot,objective,log_path,pid,process_start_identity) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    task_id,
                    agent_id,
                    "prepared",
                    task[0],
                    redact_text(canonical(config_snapshot)),
                    redact_text(canonical(profile)),
                    redact_text(objective),
                    str(log_path),
                    None,
                    None,
                ),
            )
            conn.execute(
                "UPDATE tasks SET status=?,state_version=state_version+1,updated_at=?,"
                "active_started_at=COALESCE(active_started_at,?) WHERE task_id=?",
                (TaskStatus.RUNNING, now(), time.time(), task_id),
            )
            self.emit(
                conn,
                task_id,
                "run_prepared",
                {"run_id": run_id, "agent_id": agent_id, "requirements_version": task[0]},
                run_id,
            )
        return run_id

    def recover_stale_active_period(self, task_id: str) -> float:
        """Conservatively charge elapsed wall-clock time if a process restarted mid-task."""
        now_epoch = time.time()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT active_started_at,active_seconds FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row[0] is None:
                return float(row[1])
            elapsed = max(0.0, now_epoch - float(row[0]))
            total = float(row[1]) + elapsed
            conn.execute(
                "UPDATE tasks SET active_seconds=?,active_started_at=NULL,updated_at=? WHERE task_id=?",
                (total, now(), task_id),
            )
            self.emit(
                conn,
                task_id,
                "stale_active_period_recovered",
                {"charged_seconds": round(elapsed, 3), "total_seconds": round(total, 3)},
            )
            return total

    def start_active_period(self, task_id: str) -> None:
        with self.transaction() as conn:
            row = conn.execute("SELECT active_started_at FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row[0] is None:
                conn.execute(
                    "UPDATE tasks SET active_started_at=?,updated_at=? WHERE task_id=?",
                    (time.time(), now(), task_id),
                )
                self.emit(conn, task_id, "active_period_started", {})

    def settle_active_period(self, task_id: str) -> float:
        stamp = now()
        current = time.time()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT active_started_at,active_seconds FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row[0] is None:
                return float(row[1])
            elapsed = max(0.0, current - float(row[0]))
            total = float(row[1]) + elapsed
            conn.execute(
                "UPDATE tasks SET active_seconds=?,active_started_at=NULL,updated_at=? WHERE task_id=?",
                (total, stamp, task_id),
            )
            self.emit(conn, task_id, "active_period_settled", {"seconds": round(elapsed, 3), "total": round(total, 3)})
            return total

    def active_seconds_now(self, task_id: str) -> float:
        row = self._connection.execute(
            "SELECT active_seconds,active_started_at FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(task_id)
        active = max(0.0, time.time() - float(row[1])) if row[1] is not None else 0.0
        return float(row[0]) + active

    def add_active_seconds(self, task_id: str, seconds: float) -> None:
        if seconds < 0 or not seconds < float("inf"):
            raise ValueError("Active seconds must be a finite non-negative number")
        with self.transaction() as conn:
            conn.execute(
                "UPDATE tasks SET active_seconds=active_seconds+?,updated_at=? WHERE task_id=?",
                (seconds, now(), task_id),
            )
            self.emit(conn, task_id, "active_time_recorded", {"seconds": round(seconds, 3)})

    def mark_run(
        self,
        run_id: str,
        status: str,
        *,
        pid: int | None = None,
        process_start_identity: str | None = None,
        process_command_line: str | None = None,
        executable_path: str | None = None,
        exit_code: int | None = None,
        error: str | None = None,
        event_complete: bool = False,
    ) -> None:
        with self.transaction() as conn:
            row = conn.execute("SELECT task_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            conn.execute(
                "UPDATE runs SET status=?,pid=COALESCE(?,pid),"
                "process_start_identity=COALESCE(?,process_start_identity),"
                "process_command_line=COALESCE(?,process_command_line),"
                "executable_path=COALESCE(?,executable_path),"
                "started_at=CASE WHEN ? IS NOT NULL THEN ? ELSE started_at END,"
                "ended_at=CASE WHEN ? IN ('completed','failed','timed_out','cancelled','unknown') "
                "THEN ? ELSE ended_at END,"
                "exit_code=COALESCE(?,exit_code),error=?,event_complete=? WHERE run_id=?",
                (
                    status,
                    pid,
                    process_start_identity,
                    redact_text(process_command_line) if process_command_line else None,
                    executable_path,
                    pid,
                    now(),
                    status,
                    now(),
                    exit_code,
                    error,
                    int(event_complete),
                    run_id,
                ),
            )
            self.emit(
                conn,
                row[0],
                f"run_{status}",
                {
                    "run_id": run_id,
                    "exit_code": exit_code,
                    "error": error,
                    "event_complete": event_complete,
                },
                run_id,
            )

    def add_evidence(
        self,
        task_id: str,
        requirements_version: int,
        source: str,
        summary: str,
        *,
        run_id: str | None = None,
        path: str | None = None,
        sha256: str | None = None,
        workspace_sha256: str | None = None,
        complete: bool = True,
        plan_sha256: str | None = None,
        capability_identity: str | None = None,
        verification_status: str | None = None,
    ) -> str:
        evidence_id = new_id("evd")
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO evidence(evidence_id,task_id,run_id,requirements_version,source,summary,"
                "path,sha256,workspace_sha256,complete,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    evidence_id,
                    task_id,
                    run_id,
                    requirements_version,
                    source,
                    redact_text(summary),
                    path,
                    sha256,
                    workspace_sha256,
                    int(complete),
                    now(),
                ),
            )
            conn.execute(
                "UPDATE evidence SET plan_sha256=?,capability_identity=?,verification_status=? WHERE evidence_id=?",
                (plan_sha256, capability_identity, verification_status, evidence_id),
            )
            self.emit(
                conn,
                task_id,
                "evidence_recorded",
                {
                    "evidence_id": evidence_id,
                    "source": source,
                    "requirements_version": requirements_version,
                    "complete": complete,
                },
                run_id,
            )
        return evidence_id

    def link_evidence_criteria(self, evidence_id: str, criterion_ids: list[str]) -> None:
        with self.transaction() as conn:
            for criterion_id in criterion_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO evidence_criteria(evidence_id,criterion_id) VALUES(?,?)",
                    (evidence_id, criterion_id),
                )

    def has_execution_work(self, task_id: str) -> bool:
        return self._has_execution_work(self._connection, task_id)

    @staticmethod
    def _has_execution_work(conn: sqlite3.Connection, task_id: str) -> bool:
        """Track work selected by the supervisor, without classifying user text."""
        task = conn.execute(
            "SELECT agent_calls,plan_json,criteria_json FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if task is None:
            raise KeyError(task_id)
        if task[0] or json.loads(task[1]) or json.loads(task[2]):
            return True
        if conn.execute("SELECT 1 FROM runs WHERE task_id=? LIMIT 1", (task_id,)).fetchone():
            return True
        # Applying new requirements and reconciliation clear the current plan, not its history.
        return conn.execute(
            "SELECT 1 FROM events WHERE task_id=? AND type='plan_updated' LIMIT 1", (task_id,)
        ).fetchone() is not None

    def finish_if_current(
        self,
        task_id: str,
        requirements_version: int,
        evidence_ids: list[str],
        required_criteria: list[str] | None = None,
    ) -> bool:
        with self.transaction() as conn:
            task = conn.execute(
                "SELECT requirements_version,status,criteria_json,plan_json "
                "FROM tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
            pending = conn.execute(
                "SELECT 1 FROM messages WHERE task_id=? AND applied_at IS NULL LIMIT 1", (task_id,)
            ).fetchone()
            active = conn.execute(
                "SELECT 1 FROM runs WHERE task_id=? AND status IN ('prepared','starting','running','unknown') LIMIT 1",
                (task_id,),
            ).fetchone()
            expired = conn.execute(
                "SELECT approval_id FROM approvals WHERE task_id=? AND status='pending' "
                "AND CAST(expires_at AS REAL)<=?",
                (task_id, time.time()),
            ).fetchall()
            for item in expired:
                conn.execute(
                    "UPDATE approvals SET status='expired',decision_at=?,decision_source='expired' WHERE approval_id=?",
                    (now(), item[0]),
                )
                conn.execute(
                    "UPDATE tasks SET pending_approval_id=NULL,pending_action_json=NULL,status=?,"
                    "state_version=state_version+1,updated_at=? WHERE task_id=? AND pending_approval_id=?",
                    (TaskStatus.BLOCKED, now(), task_id, item[0]),
                )
                self.emit(conn, task_id, "approval_expired", {"approval_id": item[0]})
            approval = conn.execute(
                "SELECT 1 FROM approvals WHERE task_id=? AND status='pending' LIMIT 1", (task_id,)
            ).fetchone()
            if task is None or task[0] != requirements_version or pending or active or approval:
                return False
            if task[1] != TaskStatus.VERIFYING:
                return False
            required = json.loads(task[2])
            if self._has_execution_work(conn, task_id) and (not required or not required_criteria):
                return False
            if required and required_criteria is None:
                return False
            if required_criteria is not None and set(required_criteria) != set(required):
                return False
            if not evidence_ids:
                return False
            placeholders = ",".join("?" for _ in evidence_ids)
            evidence = conn.execute(
                f"SELECT COUNT(*) FROM evidence WHERE task_id=? AND requirements_version=? AND complete=1 "
                f"AND evidence_id IN ({placeholders})",
                (task_id, requirements_version, *evidence_ids),
            ).fetchone()[0]
            if evidence != len(set(evidence_ids)):
                return False
            if required:
                fingerprint_evidence = conn.execute(
                    f"SELECT COUNT(*) FROM evidence WHERE task_id=? AND requirements_version=? "
                    f"AND evidence_id IN ({placeholders}) AND source='verification' "
                    f"AND workspace_sha256 IS NOT NULL AND plan_sha256 IS NOT NULL "
                    f"AND capability_identity IS NOT NULL AND verification_status='PASSED'",
                    (task_id, requirements_version, *evidence_ids),
                ).fetchone()[0]
                if fingerprint_evidence < len(required):
                    return False
            for criterion_id in required_criteria or []:
                satisfied = conn.execute(
                    f"SELECT 1 FROM evidence e JOIN evidence_criteria ec ON ec.evidence_id=e.evidence_id "
                    f"WHERE e.task_id=? AND e.requirements_version=? AND e.complete=1 "
                    f"AND e.source='verification' AND e.verification_status='PASSED' AND e.plan_sha256=? "
                    f"AND e.workspace_sha256 IS NOT NULL AND length(e.capability_identity)>0 "
                    f"AND EXISTS (SELECT 1 FROM runs r WHERE r.run_id=e.run_id AND r.task_id=e.task_id "
                    f"AND r.requirements_version=e.requirements_version AND r.status='completed' "
                    f"AND r.exit_code=0 AND r.event_complete=1) "
                    f"AND e.evidence_id IN ({placeholders}) AND ec.criterion_id=? LIMIT 1",
                    (
                        task_id,
                        requirements_version,
                        hashlib.sha256(task[3].encode()).hexdigest(),
                        *evidence_ids,
                        criterion_id,
                    ),
                ).fetchone()
                if not satisfied:
                    return False
            stamp = now()
            conn.execute(
                "UPDATE tasks SET status=?,state_version=state_version+1,updated_at=? WHERE task_id=?",
                (TaskStatus.SUCCEEDED, stamp, task_id),
            )
            self.emit(
                conn,
                task_id,
                "task_succeeded",
                {"requirements_version": requirements_version, "evidence_ids": evidence_ids},
            )
            return True

    def task(self, task_id: str) -> dict[str, Any] | None:
        row = self._connection.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT task_id,goal,workspace,status,requirements_version,created_at,updated_at,last_error "
            "FROM tasks ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def active_task(self) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM tasks WHERE status NOT IN ('succeeded','failed','cancelled') ORDER BY rowid LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def messages(self, task_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self._connection.execute(
                "SELECT * FROM messages WHERE task_id=? ORDER BY sequence", (task_id,)
            ).fetchall()
        ]

    def pending_messages(self, task_id: str) -> list[str]:
        return [
            row[0]
            for row in self._connection.execute(
                "SELECT text FROM messages WHERE task_id=? AND applied_at IS NULL ORDER BY sequence",
                (task_id,),
            ).fetchall()
        ]

    def events(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute("SELECT * FROM events WHERE task_id=? ORDER BY sequence", (task_id,)).fetchall()
        return [dict(row) for row in rows]

    def evidence(self, task_id: str, version: int | None = None) -> list[dict[str, Any]]:
        if version is None:
            rows = self._connection.execute(
                "SELECT * FROM evidence WHERE task_id=? ORDER BY created_at", (task_id,)
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM evidence WHERE task_id=? AND requirements_version=? ORDER BY created_at",
                (task_id, version),
            ).fetchall()
        return [dict(row) for row in rows]

    def runs(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute("SELECT * FROM runs WHERE task_id=? ORDER BY rowid", (task_id,)).fetchall()
        return [dict(row) for row in rows]

    def append_event(self, task_id: str, event_type: str, payload: dict[str, Any], run_id: str | None = None) -> None:
        with self.transaction() as conn:
            self.emit(conn, task_id, event_type, payload, run_id)

    def update_plan(self, task_id: str, plan: list[dict[str, Any]], requirements_version: int) -> None:
        all_criteria = [criterion["id"] for step in plan for criterion in step["acceptance_criteria"]]
        if len(set(all_criteria)) != len(all_criteria):
            raise ValueError("Plan criterion IDs must be globally unique")
        criteria = sorted(all_criteria)
        with self.transaction() as conn:
            row = conn.execute("SELECT requirements_version FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            pending = conn.execute(
                "SELECT 1 FROM messages WHERE task_id=? AND applied_at IS NULL LIMIT 1", (task_id,)
            ).fetchone()
            if row is None or row[0] != requirements_version or pending:
                raise RuntimeError("Plan requirements version is stale or messages are pending")
            safe_plan = redact_value(plan)
            safe_criteria = sorted({criterion["id"] for step in safe_plan for criterion in step["acceptance_criteria"]})
            conn.execute(
                "UPDATE tasks SET plan_json=?,criteria_json=?,updated_at=? WHERE task_id=?",
                (canonical(safe_plan), canonical(safe_criteria), now(), task_id),
            )
            self.emit(
                conn,
                task_id,
                "plan_updated",
                {
                    "step_count": len(plan),
                    "requirements_version": requirements_version,
                    "criterion_ids": criteria,
                },
            )

    def unresolved_runs(self) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM runs WHERE status IN ('prepared','starting','running','unknown','cancelled','timed_out') "
            "OR (status='failed' AND event_complete=0) ORDER BY rowid"
        ).fetchall()
        return [dict(row) for row in rows]

    def expire_approvals(self) -> int:
        stamp = now()
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT approval_id,task_id FROM approvals WHERE status='pending' AND CAST(expires_at AS REAL)<=?",
                (time.time(),),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE approvals SET status='expired',decision_at=?,decision_source='expired' WHERE approval_id=?",
                    (stamp, row[0]),
                )
                conn.execute(
                    "UPDATE tasks SET pending_approval_id=NULL,pending_action_json=NULL,status=?,"
                    "state_version=state_version+1,updated_at=? WHERE task_id=? AND pending_approval_id=?",
                    (TaskStatus.BLOCKED, stamp, row[1], row[0]),
                )
                self.emit(conn, row[1], "approval_expired", {"approval_id": row[0]})
            return len(rows)

    def reconcile_task_runs(self, task_id: str, *, user_confirmed: bool) -> list[str]:
        if not user_confirmed:
            raise PermissionError("Explicit user reconciliation confirmation is required")
        task = self.task(task_id)
        if task is None:
            raise ValueError("任务不存在")
        rows = self._connection.execute(
            "SELECT * FROM runs WHERE task_id=? "
            "AND (status IN ('prepared','starting','running','unknown','cancelled','timed_out') "
            "OR (status='failed' AND event_complete=0))",
            (task_id,),
        ).fetchall()
        stamp = now()
        run_ids = [row["run_id"] for row in rows]
        with self.transaction() as conn:
            for row in rows:
                conn.execute(
                    "UPDATE runs SET status='reconciled',ended_at=?,error=? WHERE run_id=?",
                    (
                        stamp,
                        "User confirmed process/workspace reconciliation; no prior action replayed",
                        row["run_id"],
                    ),
                )
            conn.execute(
                "UPDATE tasks SET status=?,last_error=?,plan_json='[]',criteria_json='[]',"
                "state_version=state_version+1,updated_at=? WHERE task_id=?",
                (
                    task["status"] if task["status"] in {"succeeded", "failed", "cancelled"} else TaskStatus.PLANNING,
                    "Prior effects reviewed; create a fresh plan before any new Run",
                    stamp,
                    task_id,
                ),
            )
            self.emit(
                conn,
                task_id,
                "runs_reconciled",
                {
                    "run_ids": run_ids,
                    "explicit_confirmation": True,
                    "plan_cleared": True,
                    "prior_plan": json.loads(task["plan_json"]),
                    "prior_runs": [dict(row) for row in rows],
                },
            )
        return run_ids

    def mark_unknown_runs(self) -> list[str]:
        rows = self._connection.execute(
            "SELECT run_id,task_id FROM runs WHERE status IN ('prepared','starting','running')"
        ).fetchall()
        run_ids: list[str] = []
        for row in rows:
            self.mark_run(row[0], "unknown", error="Process state requires reconciliation after restart")
            self.set_status(row[1], TaskStatus.INTERRUPTED, error="Run outcome is unknown; inspect side effects")
            run_ids.append(row[0])
        return run_ids

    def age_message_time(self, task_id: str) -> float:
        row = self._connection.execute(
            "SELECT MIN(received_at) FROM messages WHERE task_id=? AND applied_at IS NULL",
            (task_id,),
        ).fetchone()
        return time.time() if row is None else time.time()

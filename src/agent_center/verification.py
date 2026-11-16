from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import stat
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agent_center.adapters.base import AgentRunResult
from agent_center.capabilities import installation_identity, required_capabilities
from agent_center.config import AppConfiguration
from agent_center.domain import TaskStatus
from agent_center.storage.database import Store

VerificationLauncher = Callable[
    [str, str, int, str, tuple[str, ...], asyncio.Event | None], Awaitable[tuple[str, AgentRunResult]]
]


class VerificationError(RuntimeError):
    """Raised when an independent verification cannot be safely performed."""


class VerificationService:
    """Coordinate independent Runs through native Agent CLIs; never execute project code."""

    _MAX_FINGERPRINT_BYTES = 256 * 1024 * 1024
    PYTEST_PREFIX = (
        "/usr/bin/env",
        "PYTEST_ADDOPTS=",
        "PYTEST_PLUGINS=",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
        "python",
        "-B",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-c",
        "/dev/null",
        "-o",
        "addopts=",
        "-q",
    )

    def __init__(
        self, store: Store, config: AppConfiguration | None = None, launch: VerificationLauncher | None = None
    ):
        self.store = store
        self.config = config
        self.launch = launch

    @classmethod
    def workspace_fingerprint(cls, workspace: Path) -> str:
        root = workspace.resolve(strict=True)
        digest = hashlib.sha256()
        consumed = 0
        entries = 0

        def unreadable(exc: OSError) -> None:
            raise VerificationError("workspace contains an unreadable directory") from exc

        def record(kind: str, relative: str, mode: int, value: str = "") -> None:
            nonlocal entries
            entries += 1
            if entries > 100_000:
                raise VerificationError("workspace exceeds the evidence fingerprint entry limit")
            digest.update(json.dumps([kind, relative, mode, value], ensure_ascii=True).encode() + b"\n")

        try:
            for current, dirs, files in os.walk(root, topdown=True, followlinks=False, onerror=unreadable):
                dirs.sort()
                directory = Path(current)
                if directory.is_symlink():
                    raise VerificationError("workspace directory changed while fingerprinting")
                record("D", directory.relative_to(root).as_posix(), directory.stat().st_mode)
                links = [name for name in dirs if (directory / name).is_symlink()]
                for name in sorted([*files, *links]):
                    path = directory / name
                    relative = path.relative_to(root).as_posix()
                    info = path.lstat()
                    if stat.S_ISLNK(info.st_mode):
                        record("L", relative, info.st_mode, os.readlink(path))
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        raise VerificationError("workspace contains an unsupported special file")
                    file_digest = hashlib.sha256()
                    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
                        before = os.fstat(stream.fileno())
                        while chunk := stream.read(1024 * 1024):
                            consumed += len(chunk)
                            if consumed > cls._MAX_FINGERPRINT_BYTES:
                                raise VerificationError("workspace exceeds the evidence fingerprint byte limit")
                            file_digest.update(chunk)
                        after = os.fstat(stream.fileno())
                    if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_mode) != (
                        after.st_ino,
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_mode,
                    ):
                        raise VerificationError("workspace changed while fingerprinting")
                    record("F", relative, after.st_mode, file_digest.hexdigest())
        except OSError as exc:
            raise VerificationError("workspace changed or could not be read while fingerprinting") from exc
        return digest.hexdigest()

    @staticmethod
    def plan_hash(task: dict[str, Any]) -> str:
        return hashlib.sha256(task["plan_json"].encode()).hexdigest()

    @staticmethod
    def validate_command(argv: list[str], workspace: Path) -> tuple[str, ...]:
        prefix = list(VerificationService.PYTEST_PREFIX)
        if argv[: len(prefix)] != prefix or len(argv) <= len(prefix):
            raise VerificationError("Plan must save the fixed pytest environment/config argv and explicit test files")
        for name in argv[len(prefix) :]:
            if not re.fullmatch(r"[A-Za-z0-9_./-]+\.py", name) or name.startswith(("-", "/")):
                raise VerificationError("Only explicit relative Python test filenames are supported")
            path = workspace / name
            if ".." in Path(name).parts or any(p.is_symlink() for p in (path, *path.parents)):
                raise VerificationError("Test paths cannot traverse symlinks or parent directories")
            if not path.resolve(strict=True).is_relative_to(workspace) or not path.is_file():
                raise VerificationError("Registered test file is outside the workspace or unavailable")
        return tuple(argv)

    async def run_for_criteria(
        self,
        task_id: str,
        requirements_version: int,
        criteria_ids: list[str],
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> list[str]:
        task = self.store.task(task_id)
        if not task or task["requirements_version"] != requirements_version or self.store.pending_messages(task_id):
            raise VerificationError("Verification requirements are stale or pending")
        plan_hash = self.plan_hash(task)
        criteria = {c["id"]: c for s in json.loads(task["plan_json"]) for c in s["acceptance_criteria"]}
        if (
            not criteria_ids
            or len(set(criteria_ids)) != len(criteria_ids)
            or any(c not in criteria for c in criteria_ids)
        ):
            raise VerificationError("Criterion IDs must belong to the current saved Plan")
        if self.config is None or self.launch is None:
            raise VerificationError("UNVERIFIED: native Agent verification launcher is not configured")
        workspace = Path(task["workspace"]).resolve(strict=True)
        # Validate every command before reserving a call or creating a Run.
        commands = {cid: self.validate_command(criteria[cid]["verification_argv"], workspace) for cid in criteria_ids}
        selected: str | None = None
        for agent_id, agent in self.config.agents.items():
            profile = self.config.profiles[agent.default_profile]
            if not agent.enabled or not agent.allow_execution or profile.permission_mode != "read-only":
                continue
            if agent.capabilities.permits(
                installation_identity(agent, profile), required_capabilities("read-only", verification=True)
            ):
                selected = agent_id
                break
        if selected is None:
            self.store.append_event(
                task_id, "verification_unverified", {"status": "UNVERIFIED", "criteria": criteria_ids}
            )
            raise VerificationError(
                "UNVERIFIED: no Agent has current, verified native read-only verification capabilities"
            )
        agent = self.config.agents[selected]
        profile = self.config.profiles[agent.default_profile]
        identity = installation_identity(agent, profile)
        evidence_ids: list[str] = []
        for cid, command in commands.items():
            current = self.store.task(task_id)
            if (
                not current
                or current["requirements_version"] != requirements_version
                or self.plan_hash(current) != plan_hash
                or self.store.pending_messages(task_id)
            ):
                raise VerificationError("Plan changed before verification")
            before = self.workspace_fingerprint(workspace)
            run_id, result = await self.launch(task_id, selected, requirements_version, cid, command, cancel_event)
            after = self.workspace_fingerprint(workspace)
            current = self.store.task(task_id)
            stable = bool(
                current
                and current["requirements_version"] == requirements_version
                and self.plan_hash(current) == plan_hash
                and not self.store.pending_messages(task_id)
                and before == after
                and installation_identity(agent, profile) == identity
            )
            status = self.command_status(result, command, workspace) if stable else "UNVERIFIED"
            command_details = []
            for executed in result.commands:
                detail = asdict(executed)
                if executed.output is not None:
                    detail["output_sha256"] = hashlib.sha256(executed.output.encode()).hexdigest()
                    detail["output"] = executed.output[:8000]
                command_details.append(detail)
            details = {
                "status": status,
                "criterion_id": cid,
                "argv": command,
                "cwd": str(workspace),
                "process_exit": result.process.returncode,
                "error": result.error,
                "commands": command_details,
            }
            eid = self.store.add_evidence(
                task_id,
                requirements_version,
                "verification",
                json.dumps(details, ensure_ascii=False),
                run_id=run_id,
                complete=status == "PASSED",
                workspace_sha256=after,
                plan_sha256=plan_hash,
                capability_identity=identity,
                verification_status=status,
            )
            self.store.link_evidence_criteria(eid, [cid])
            evidence_ids.append(eid)
            if status == "UNVERIFIED":
                raise VerificationError(
                    f"{status}: native execution evidence did not prove criterion {cid}; run={run_id}"
                )
            self.store.set_status(task_id, TaskStatus.VERIFYING)
        return evidence_ids

    @staticmethod
    def command_status(result: AgentRunResult, command: tuple[str, ...], workspace: Path) -> str:
        if (
            not result.completed
            or result.process.returncode != 0
            or result.process.timed_out
            or result.process.cancelled
            or result.process.truncated
            or result.approval_requests
        ):
            return "UNVERIFIED"
        # Additional tool commands, shell wrappers, inferred cwd, or text reports cannot attest a criterion.
        if len(result.commands) != 1:
            return "UNVERIFIED"
        for event in result.events:
            if event.kind == "item.completed":
                item = event.raw.get("item", {})
                if item.get("type") not in {"command_execution", "agent_message", "assistant_message", "reasoning"}:
                    return "UNVERIFIED"
        actual = result.commands[0]
        matches = actual.command == shlex.join(command)
        if (
            not matches
            or actual.cwd != str(workspace)
            or not actual.finished
            or actual.status != "completed"
            or actual.exit_code is None
            or actual.output is None
            or not actual.event_id
        ):
            return "UNVERIFIED"
        return "PASSED" if actual.exit_code == 0 else "FAILED"

    def is_fresh(self, task_id: str, evidence_ids: list[str]) -> bool:
        task = self.store.task(task_id)
        if not task or self.config is None or self.store.pending_messages(task_id):
            return False
        try:
            current = self.workspace_fingerprint(Path(task["workspace"]))
        except OSError, VerificationError:
            return False
        rows = {row["evidence_id"]: row for row in self.store.evidence(task_id)}
        runs = {row["run_id"]: row for row in self.store.runs(task_id)}
        for eid in evidence_ids:
            row = rows.get(eid)
            if (
                not row
                or not row["complete"]
                or row["verification_status"] != "PASSED"
                or row["requirements_version"] != task["requirements_version"]
                or row["plan_sha256"] != self.plan_hash(task)
                or row["workspace_sha256"] != current
            ):
                return False
            run = runs.get(row["run_id"])
            if not run or run["status"] != "completed" or run["exit_code"] != 0 or not run["event_complete"]:
                return False
            agent = self.config.agents.get(run["agent_id"])
            if not agent or not agent.enabled or not agent.allow_execution:
                return False
            profile = self.config.profiles[agent.default_profile]
            identity = installation_identity(agent, profile)
            if row["capability_identity"] != identity or not agent.capabilities.permits(
                identity, required_capabilities("read-only", verification=True)
            ):
                return False
        return bool(evidence_ids)

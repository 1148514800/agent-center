from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from agent_center.domain import Criterion
from agent_center.runtime.process import ProcessRunner
from agent_center.storage.database import Store


class VerificationError(RuntimeError):
    pass


class VerificationService:
    """Run only saved, bounded test commands and bind evidence to workspace contents."""

    _IGNORED_DIRS = {".git", ".pytest_cache", "__pycache__", ".venv", "node_modules"}
    _MAX_FINGERPRINT_BYTES = 256 * 1024 * 1024

    def __init__(self, store: Store, runner: ProcessRunner):
        self.store = store
        self.runner = runner

    @classmethod
    def workspace_fingerprint(cls, workspace: Path) -> str:
        root = workspace.resolve(strict=True)
        digest = hashlib.sha256()
        consumed = 0
        for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
            dirs[:] = sorted(name for name in dirs if name not in cls._IGNORED_DIRS)
            for name in sorted(files):
                path = Path(current) / name
                relative = path.relative_to(root).as_posix()
                if path.is_symlink():
                    record = os.readlink(path).encode("utf-8", errors="replace")
                    digest.update(b"L\0" + relative.encode() + b"\0" + record + b"\0")
                    continue
                if not path.is_file():
                    continue
                try:
                    size = path.stat().st_size
                    consumed += size
                    if consumed > cls._MAX_FINGERPRINT_BYTES:
                        raise VerificationError("workspace exceeds the evidence fingerprint byte limit")
                    digest.update(b"F\0" + relative.encode("utf-8") + b"\0")
                    with path.open("rb") as stream:
                        while chunk := stream.read(1024 * 1024):
                            digest.update(chunk)
                    digest.update(b"\0")
                except OSError as exc:
                    raise VerificationError("workspace changed while its evidence fingerprint was read") from exc
        return digest.hexdigest()

    def _resolve_argv(self, argv: list[str], workspace: Path) -> list[str]:
        if len(argv) < 4 or len(argv) > 10:
            raise VerificationError("verification command shape is invalid")
        if any("\n" in arg or "\x00" in arg for arg in argv):
            raise VerificationError("control characters are not allowed")
        if Path(argv[0]).name not in {"pytest", "python", "python3", "python3.14"}:
            raise VerificationError("verification is limited to the current Python pytest installation")
        if Path(argv[0]).name == "pytest":
            pytest_args = argv[1:]
        elif argv[1:3] == ["-m", "pytest"]:
            pytest_args = argv[3:]
        else:
            raise VerificationError("Python verification must use -m pytest")
        # Preserve the venv path: resolving .venv/bin/python breaks pyvenv.cfg detection.
        binary = Path(sys.executable)
        if not pytest_args or pytest_args[0] not in {"-q", "--quiet"}:
            raise VerificationError("pytest must use quiet mode")
        test_args = pytest_args[1:]
        if not test_args or any(arg.startswith("-") for arg in test_args):
            raise VerificationError("pytest requires explicit test files and no other options")
        resolved_files: list[str] = []
        test_root = (workspace / "tests").resolve(strict=False)
        for raw in test_args:
            candidate = workspace / raw
            path = candidate.resolve(strict=True)
            try:
                path.relative_to(test_root)
            except ValueError as exc:
                raise VerificationError("verification may only target explicit files under workspace/tests") from exc
            if candidate.is_symlink() or not path.is_file() or not path.name.startswith("test_"):
                raise VerificationError("verification target must be a regular test_*.py file")
            if path.suffix != ".py":
                raise VerificationError("only Python test files are accepted")
            resolved_files.append(str(path.relative_to(workspace)))
        return [str(binary), "-m", "pytest", "-q", "-p", "no:cacheprovider", *resolved_files]

    async def run_for_criteria(self, task_id: str, requirements_version: int, criteria_ids: list[str]) -> list[str]:
        task = self.store.task(task_id)
        if not task:
            raise VerificationError("task does not exist")
        plan = json.loads(task["plan_json"])
        criteria = {
            criterion["id"]: Criterion.model_validate(criterion)
            for step in plan
            for criterion in step["acceptance_criteria"]
        }
        if not criteria_ids or not set(criteria_ids).issubset(criteria):
            raise VerificationError("criterion is not in the current plan")
        workspace = Path(task["workspace"]).resolve(strict=True)
        evidence_ids: list[str] = []
        for criterion_id in sorted(set(criteria_ids)):
            criterion = criteria[criterion_id]
            argv = self._resolve_argv(criterion.verification_argv, workspace)
            before_fingerprint = self.workspace_fingerprint(workspace)
            log_dir = self.store.data_dir / "tasks" / task_id / "verification"
            log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            run_id = f"verify_{os.urandom(6).hex()}"
            stdout = log_dir / f"{run_id}.stdout"
            stderr = log_dir / f"{run_id}.stderr"
            result = await self.runner.run(
                argv[0],
                argv[1:],
                cwd=workspace,
                env={
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "PYTHONPATH": str(workspace),
                    "HOME": str(Path.home()),
                    "LANG": "en_US.UTF-8",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                    "PYTEST_ADDOPTS": "",
                    "TERM": "dumb",
                },
                stdin=b"",
                timeout=300,
                stdout_path=stdout,
                stderr_path=stderr,
            )
            if result.timed_out or result.truncated:
                raise VerificationError(f"verification for {criterion_id} was truncated or timed out")
            if result.returncode != 0:
                raise VerificationError(f"verification for {criterion_id} exited with status {result.returncode}")
            after_fingerprint = self.workspace_fingerprint(workspace)
            if before_fingerprint != after_fingerprint:
                raise VerificationError(
                    f"verification changed workspace files for {criterion_id}; result is not accepted"
                )
            body = (stdout.read_text(errors="replace") + "\n" + stderr.read_text(errors="replace"))[:6000]
            digest = hashlib.sha256(body.encode()).hexdigest()
            evidence_id = self.store.add_evidence(
                task_id,
                requirements_version,
                "verification",
                f"criterion={criterion_id}; command={argv}; exit=0; output_sha256={digest}; "
                f"workspace_sha256={after_fingerprint}; output={body}",
                run_id=run_id,
                path=str(stdout),
                sha256=digest,
                workspace_sha256=after_fingerprint,
                complete=True,
            )
            self.store.link_evidence_criteria(evidence_id, [criterion_id])
            self.store.append_event(
                task_id,
                "verification_run",
                {
                    "evidence_id": evidence_id,
                    "criterion_id": criterion_id,
                    "argv": argv,
                    "exit_code": 0,
                    "requirements_version": requirements_version,
                    "workspace_sha256": after_fingerprint,
                },
            )
            evidence_ids.append(evidence_id)
        return evidence_ids

    def is_fresh(self, task_id: str, evidence_ids: list[str]) -> bool:
        task = self.store.task(task_id)
        if not task:
            return False
        try:
            current = self.workspace_fingerprint(Path(task["workspace"]))
        except OSError, VerificationError:
            return False
        rows = {row["evidence_id"]: row for row in self.store.evidence(task_id)}
        return all(
            evidence_id in rows
            and rows[evidence_id]["source"] == "verification"
            and rows[evidence_id]["workspace_sha256"] == current
            for evidence_id in evidence_ids
        )

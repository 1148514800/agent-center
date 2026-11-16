from __future__ import annotations

import asyncio
import json
import shlex
from dataclasses import replace
from pathlib import Path

import pytest

from agent_center.adapters.base import AgentRunResult, CommandExecution
from agent_center.capabilities import AgentCapabilities, installation_identity, required_capabilities
from agent_center.config import AppConfiguration
from agent_center.domain import TaskStatus
from agent_center.runtime.process import ProcessResult
from agent_center.storage.database import Store
from agent_center.verification import VerificationError, VerificationService

ARGV = (
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
    "tests/test_ok.py",
)


def save_plan(store: Store, root: Path) -> str:
    (root / "tests").mkdir()
    (root / "tests/test_ok.py").write_text("raise RuntimeError('must never run on host')\n")
    task = store.create_task("Implement feature", root, {})
    store.update_plan(
        task,
        [
            {
                "id": "s",
                "objective": "Implement feature",
                "acceptance_criteria": [
                    {"id": "c", "description": "Test passes", "verification_argv": list(ARGV)},
                ],
            }
        ],
        1,
    )
    return task


def result(root: Path) -> AgentRunResult:
    return AgentRunResult(
        ProcessResult(0, 10, 0, False, False, False),
        (),
        True,
        None,
        "All tests pass!",
        (CommandExecution(shlex.join(ARGV), str(root), 0, "completed", "1 passed", "cmd-1", True),),
    )


@pytest.mark.parametrize(
    "field,value,status",
    [
        ("command", "python -m pytest; echo ok", "UNVERIFIED"),
        ("cwd", "/elsewhere", "UNVERIFIED"),
        ("cwd", None, "UNVERIFIED"),
        ("exit_code", None, "UNVERIFIED"),
        ("exit_code", 1, "FAILED"),
        ("status", "in_progress", "UNVERIFIED"),
        ("output", None, "UNVERIFIED"),
        ("finished", False, "UNVERIFIED"),
    ],
)
def test_exact_command_evidence(field: str, value: object, status: str, tmp_path: Path) -> None:
    good = result(tmp_path)
    changed = replace(good.commands[0], **{field: value})
    assert VerificationService.command_status(replace(good, commands=(changed,)), ARGV, tmp_path) == status


@pytest.mark.parametrize("flag", ["timed_out", "cancelled", "truncated"])
def test_interrupted_cli_cannot_pass(flag: str, tmp_path: Path) -> None:
    good = result(tmp_path)
    process = replace(good.process, **{flag: True})
    assert VerificationService.command_status(replace(good, process=process), ARGV, tmp_path) == "UNVERIFIED"


def test_text_is_never_test_evidence(tmp_path: Path) -> None:
    assert VerificationService.command_status(replace(result(tmp_path), commands=()), ARGV, tmp_path) == "UNVERIFIED"


@pytest.mark.parametrize("suffix", [[";", "echo", "ok"], ["--override-ini=x"], ["../test.py"], ["tests"]])
def test_plan_command_allowlist(suffix: list[str], tmp_path: Path) -> None:
    with pytest.raises((VerificationError, FileNotFoundError)):
        VerificationService.validate_command(list(ARGV[:-1]) + suffix, tmp_path)


def test_symlink_test_path_refused(tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    (target / "test_ok.py").write_text("pass")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "tests").symlink_to(target, target_is_directory=True)
    with pytest.raises(VerificationError, match="symlinks"):
        VerificationService.validate_command(list(ARGV), workspace)


def test_evidence_binds_run_plan_version_workspace_and_capabilities(
    store: Store,
    verified_config: AppConfiguration,
    tmp_path: Path,
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    task = save_plan(store, root)
    calls = []

    async def launch(task_id, agent, version, cid, command, cancel):
        calls.append((agent, cid, command))
        run = store.create_run(
            task_id, agent, "Independent verification", {}, root / "unused.log", {}, expected_version=version
        )
        store.mark_run(run, "completed", exit_code=0, event_complete=True)
        return run, result(root)

    verifier = VerificationService(store, verified_config, launch)
    ids = asyncio.run(verifier.run_for_criteria(task, 1, ["c"]))
    evidence = store.evidence(task)[0]
    assert evidence["verification_status"] == "PASSED"
    assert evidence["run_id"] == store.runs(task)[0]["run_id"]
    assert evidence["plan_sha256"] and evidence["capability_identity"]
    assert verifier.is_fresh(task, ids)
    # Changing a saved plan without changing requirements version still invalidates evidence.
    with store.transaction() as conn:
        conn.execute("UPDATE tasks SET plan_json=plan_json || ' ' WHERE task_id=?", (task,))
    assert not verifier.is_fresh(task, ids)
    with store.transaction() as conn:
        conn.execute("UPDATE tasks SET plan_json=rtrim(plan_json) WHERE task_id=?", (task,))
    (root / "tests/test_ok.py").write_text("changed")
    assert not verifier.is_fresh(task, ids)
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["no_command", "process_failure", "mutation", "wrong_cwd"])
def test_unproven_results_are_saved_unverified(
    store: Store,
    verified_config: AppConfiguration,
    tmp_path: Path,
    failure: str,
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    task = save_plan(store, root)

    async def launch(task_id, agent, version, cid, command, cancel):
        run = store.create_run(task_id, agent, "verification", {}, root / "unused", {}, expected_version=version)
        parsed = result(root)
        if failure == "no_command":
            parsed = replace(parsed, commands=())
        if failure == "process_failure":
            parsed = replace(parsed, completed=False, process=replace(parsed.process, returncode=1))
        if failure == "mutation":
            (root / "modified").write_text("changed")
        if failure == "wrong_cwd":
            parsed = replace(parsed, commands=(replace(parsed.commands[0], cwd="/unknown"),))
        store.mark_run(
            run,
            "completed" if parsed.completed else "failed",
            exit_code=parsed.process.returncode,
            event_complete=parsed.completed,
        )
        return run, parsed

    verifier = VerificationService(store, verified_config, launch)
    with pytest.raises(VerificationError, match="UNVERIFIED"):
        asyncio.run(verifier.run_for_criteria(task, 1, ["c"]))
    evidence = store.evidence(task)[0]
    assert evidence["verification_status"] == "UNVERIFIED" and not evidence["complete"]
    assert json.loads(evidence["summary"])["criterion_id"] == "c"
    assert not store.finish_if_current(task, 1, [evidence["evidence_id"]], ["c"])


def test_capabilities_expire_on_install_or_permission_change(verified_config: AppConfiguration) -> None:
    agent = verified_config.agents["codex"]
    profile = verified_config.profiles[agent.default_profile]
    identity = installation_identity(agent, profile)
    assert agent.capabilities.permits(identity, required_capabilities("read-only", verification=True))
    assert not AgentCapabilities().permits(identity, required_capabilities("read-only"))
    assert not agent.capabilities.permits(
        installation_identity(agent, profile.model_copy(update={"permission_mode": "workspace-write"})),
        required_capabilities("workspace-write"),
    )
    Path(agent.executable).write_text("#!/bin/sh\nexit 1\n")
    assert not agent.capabilities.permits(installation_identity(agent, profile), required_capabilities("read-only"))


def test_no_permission_fallback(store: Store, config: AppConfiguration, tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    task = save_plan(store, root)

    async def launch(*args):
        pytest.fail("Unverified agent must never be launched")

    verifier = VerificationService(store, config, launch)
    with pytest.raises(VerificationError, match="no Agent"):
        asyncio.run(verifier.run_for_criteria(task, 1, ["c"]))
    assert store.runs(task) == []
    store.set_status(task, TaskStatus.VERIFYING)


def test_capability_change_invalidates_previously_passed_evidence(
    store: Store,
    verified_config: AppConfiguration,
    tmp_path: Path,
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    task = save_plan(store, root)

    async def launch(task_id, agent, version, cid, command, cancel):
        run = store.create_run(task_id, agent, "verification", {}, root / "unused", {}, expected_version=version)
        store.mark_run(run, "completed", exit_code=0, event_complete=True)
        return run, result(root)

    verifier = VerificationService(store, verified_config, launch)
    ids = asyncio.run(verifier.run_for_criteria(task, 1, ["c"]))
    assert verifier.is_fresh(task, ids)
    Path(verified_config.agents["codex"].executable).write_text("changed CLI version")
    assert not verifier.is_fresh(task, ids)


def test_fingerprint_tracks_modes_empty_directories_and_symlink_targets(tmp_path: Path) -> None:
    source = tmp_path / "file"
    source.write_text("same content")
    first = VerificationService.workspace_fingerprint(tmp_path)
    source.chmod(0o700)
    second = VerificationService.workspace_fingerprint(tmp_path)
    assert first != second
    (tmp_path / "empty").mkdir()
    third = VerificationService.workspace_fingerprint(tmp_path)
    assert second != third
    (tmp_path / "link").symlink_to("file")
    fourth = VerificationService.workspace_fingerprint(tmp_path)
    (tmp_path / "link").unlink()
    (tmp_path / "link").symlink_to("elsewhere")
    assert fourth != VerificationService.workspace_fingerprint(tmp_path)


def test_shell_newline_cannot_match_saved_argv(tmp_path: Path) -> None:
    good = result(tmp_path)
    injected = replace(good.commands[0], command=good.commands[0].command.replace(" -B", "\n-B"))
    assert VerificationService.command_status(replace(good, commands=(injected,)), ARGV, tmp_path) == "UNVERIFIED"

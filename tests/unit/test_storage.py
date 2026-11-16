from __future__ import annotations

from pathlib import Path
from time import sleep

from agent_center.domain import TaskStatus
from agent_center.storage.database import Store


def test_stop_resume_clock_preserves_elapsed_time_across_restart(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(data_dir)
    task_id = store.create_task("What is a task?", workspace, {})
    store.start_active_period(task_id)
    sleep(0.02)
    before_restart = store.active_seconds_now(task_id)
    assert before_restart > 0
    store.close()

    restored = Store(data_dir)
    try:
        assert restored.active_seconds_now(task_id) >= before_restart
        settled = restored.settle_active_period(task_id)
        assert settled >= before_restart
        assert restored.task(task_id)["active_started_at"] is None
    finally:
        restored.close()


def test_stale_active_period_is_charged_on_recovery(tmp_path: Path) -> None:
    import time

    data_dir = tmp_path / "data"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(data_dir)
    task_id = store.create_task("What is a task?", workspace, {})
    store.start_active_period(task_id)
    with store.transaction() as conn:
        conn.execute("UPDATE tasks SET active_started_at=? WHERE task_id=?", (time.time() - 60, task_id))
    assert store.recover_stale_active_period(task_id) >= 60
    assert store.task(task_id)["active_started_at"] is None
    store.close()


def test_plan_rejects_criterion_ids_reused_across_steps(store: Store, tmp_path: Path) -> None:
    from agent_center.domain import Criterion, PlanStep

    task_id = store.create_task("Implement a feature", tmp_path, {})
    criterion_a = Criterion(
        id="same-id",
        description="first check",
        verification_argv=["python", "-m", "pytest", "-q", "tests/test_first.py"],
    )
    criterion_b = Criterion(
        id="same-id",
        description="second check",
        verification_argv=["python", "-m", "pytest", "-q", "tests/test_second.py"],
    )
    plan = [
        PlanStep(id="step-a", objective="first", acceptance_criteria=[criterion_a]),
        PlanStep(id="step-b", objective="second", acceptance_criteria=[criterion_b]),
    ]
    try:
        store.update_plan(task_id, [step.model_dump(mode="json") for step in plan], 1)
    except ValueError as exc:
        assert "globally unique" in str(exc)
    else:
        raise AssertionError("duplicate criterion IDs must be rejected")


def test_event_and_state_commit_atomically(store: Store, tmp_path: Path) -> None:
    task_id = store.create_task("hello", tmp_path, {"redacted": True})
    before = len(store.events(task_id))
    try:
        with store.transaction() as conn:
            conn.execute("UPDATE tasks SET status=? WHERE task_id=?", (TaskStatus.RUNNING, task_id))
            raise RuntimeError("forced")
    except RuntimeError:
        pass
    assert store.task(task_id)["status"] == TaskStatus.CREATED
    assert len(store.events(task_id)) == before


def test_unapplied_message_invalidates_succeeded_finalization(store: Store, tmp_path: Path) -> None:
    task_id = store.create_task("goal", tmp_path, {})
    version = 1
    evidence_id = store.add_evidence(task_id, version, "supervisor", "answer")
    store.add_message(task_id, "new constraint")
    assert not store.finish_if_current(task_id, version, [evidence_id])
    assert store.apply_pending_messages(task_id) == 2
    assert not store.finish_if_current(task_id, version, [evidence_id])
    assert store.task(task_id)["status"] != TaskStatus.SUCCEEDED


def test_direct_answer_needs_verifying_status(store: Store, tmp_path: Path) -> None:
    task_id = store.create_task("What is 6x7?", tmp_path, {})
    evidence_id = store.add_evidence(task_id, 1, "supervisor", "42")
    assert not store.finish_if_current(task_id, 1, [evidence_id])
    store.set_status(task_id, TaskStatus.VERIFYING)
    assert store.finish_if_current(task_id, 1, [evidence_id])


def test_direct_answer_cannot_bypass_saved_plan_criteria(store: Store, tmp_path: Path) -> None:
    from agent_center.domain import Criterion, PlanStep

    task_id = store.create_task("What is a good implementation?", tmp_path, {})
    step = PlanStep(
        id="s1",
        objective="implement this feature",
        acceptance_criteria=[
            Criterion(
                id="c1",
                description="feature test passes",
                verification_argv=["python", "-m", "pytest", "-q", "tests/test_feature.py"],
            )
        ],
    )
    store.update_plan(task_id, [step.model_dump(mode="json")], 1)
    store.set_status(task_id, TaskStatus.VERIFYING)
    evidence_id = store.add_evidence(task_id, 1, "supervisor", "looks good")
    assert not store.finish_if_current(task_id, 1, [evidence_id])


def test_only_complete_current_evidence_can_finish(store: Store, tmp_path: Path) -> None:
    task_id = store.create_task("goal", tmp_path, {})
    stale = store.add_evidence(task_id, 0, "supervisor", "stale")
    assert not store.finish_if_current(task_id, 1, [stale])
    incomplete = store.add_evidence(task_id, 1, "agent_event", "truncated", complete=False)
    assert not store.finish_if_current(task_id, 1, [incomplete])
    current = store.add_evidence(task_id, 1, "supervisor", "answer")
    store.set_status(task_id, TaskStatus.VERIFYING)
    assert not store.finish_if_current(task_id, 1, [current])
    assert store.task(task_id)["status"] == TaskStatus.VERIFYING


def test_ambiguous_run_becomes_unknown_not_replayed(store: Store, tmp_path: Path) -> None:
    from agent_center.domain import Criterion, PlanStep

    task_id = store.create_task("implement a synthetic fixture", tmp_path, {})
    step = PlanStep(
        id="s1",
        objective="synthetic",
        acceptance_criteria=[
            Criterion(
                id="c1",
                description="test",
                verification_argv=["python", "-m", "pytest", "-q", "tests/test_fixture.py"],
            )
        ],
    )
    store.update_plan(task_id, [step.model_dump(mode="json")], 1)
    store.set_status(task_id, TaskStatus.PLANNING)
    run_id = store.create_run(
        task_id,
        "codex",
        "write something",
        {"model": "test"},
        tmp_path / "stdout",
        {},
        expected_version=1,
    )
    assert store.mark_unknown_runs() == [run_id]
    run = store.runs(task_id)[0]
    assert run["status"] == "unknown"
    assert store.task(task_id)["status"] == TaskStatus.INTERRUPTED
    try:
        store.create_run(task_id, "codex", "retry", {}, tmp_path / "out2", {}, expected_version=1)
    except RuntimeError as exc:
        assert "reconcile" in str(exc)
    else:
        raise AssertionError("unknown side-effect run must prevent automatic retry")
    try:
        store.reconcile_task_runs(task_id, user_confirmed=False)
    except PermissionError:
        pass
    else:
        raise AssertionError("reconciliation without user confirmation must be rejected")
    assert store.reconcile_task_runs(task_id, user_confirmed=True) == [run_id]
    assert store.runs(task_id)[0]["status"] == "reconciled"

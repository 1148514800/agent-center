from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_center.domain import Criterion, PlanStep
from agent_center.storage.database import Store
from agent_center.verification import VerificationError, VerificationService


def test_untrusted_project_tests_fail_closed_before_creating_logs_or_spawning(store: Store, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    tests = workspace / "tests"
    tests.mkdir(parents=True)
    sentinel = tmp_path / "external-sentinel"
    (tests / "test_sentinel.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    task_id = store.create_task("Implement a feature", workspace, {})
    criterion = Criterion(
        id="c1",
        description="synthetic test",
        verification_argv=["python", "-m", "pytest", "-q", "tests/test_sentinel.py"],
    )
    store.update_plan(
        task_id,
        [PlanStep(id="s1", objective="implement", acceptance_criteria=[criterion]).model_dump(mode="json")],
        1,
    )
    outside = tmp_path / "outside-test.py"
    outside.write_text("def test_outside(): pass\n", encoding="utf-8")
    (tests / "test_link.py").symlink_to(outside)
    verifier = VerificationService(store)
    with pytest.raises(VerificationError, match="UNVERIFIED"):
        asyncio.run(verifier.run_for_criteria(task_id, 1, ["c1"]))
    with pytest.raises(VerificationError, match="current saved Plan"):
        asyncio.run(verifier.run_for_criteria(task_id, 1, ["../outside-test.py"]))
    assert not sentinel.exists()
    assert store.evidence(task_id) == []
    assert not (store.data_dir / "tasks" / task_id / "verification").exists()

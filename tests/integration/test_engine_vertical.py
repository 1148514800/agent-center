from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from agent_center.config import AppConfiguration
from agent_center.domain import Criterion, Delegate, PlanStep, UpdatePlan, Verify
from agent_center.engine import TaskEngine
from agent_center.storage.database import Store


class ScriptedSupervisor:
    def __init__(self, step: PlanStep):
        self.actions: list[Any] = [
            UpdatePlan(action="update_plan", reason_summary="synthetic plan", steps=[step]),
            Delegate(
                action="delegate",
                reason_summary="synthetic execution",
                agent_id="codex",
                step_id=step.id,
                objective=step.objective,
                acceptance_criteria=step.acceptance_criteria,
            ),
            Verify(
                action="verify",
                reason_summary="attempt independent test",
                criterion_ids=[criterion.id for criterion in step.acceptance_criteria],
            ),
        ]

    async def decide(self, **_: Any) -> Any:
        return self.actions.pop(0)


def test_synthetic_agent_run_cannot_execute_unisolated_project_tests(
    config: AppConfiguration, store: Store, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    tests = workspace / "tests"
    tests.mkdir(parents=True)
    (workspace / "answer.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    marker = tmp_path / "sentinel-should-not-be-created"
    (tests / "test_answer.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\ndef test_answer(): assert True\n",
        encoding="utf-8",
    )
    fake_cli = tmp_path / "fake-codex"
    fake_cli.write_text(
        f"#!{os.sys.executable}\n"
        "import json\n"
        "for event in ["
        "{'type':'thread.started','thread_id':'synthetic'},"
        "{'type':'item.completed','item':{'type':'agent_message','text':'Synthetic only.'}},"
        "{'type':'turn.completed'}]: print(json.dumps(event), flush=True)\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o700)
    agents = {
        "codex": config.agents["codex"].model_copy(
            update={"executable": str(fake_cli), "allow_execution": True, "capability_status": "supported"}
        ),
        "deepseek": config.agents["deepseek"].model_copy(update={"allow_execution": False}),
    }
    config = config.model_copy(update={"agents": agents})
    criterion = Criterion(
        id="answer-test",
        description="The answer test must pass.",
        verification_argv=["python", "-m", "pytest", "-q", "tests/test_answer.py"],
    )
    step = PlanStep(id="implement-answer", objective="Implement the answer function.", acceptance_criteria=[criterion])
    task_id, _ = TaskEngine(config, store, model_client=ScriptedSupervisor(step)).create_task(
        "Implement a small answer function in this workspace.", workspace
    )
    engine = TaskEngine(config, store, model_client=ScriptedSupervisor(step))
    result = asyncio.run(engine.run_task(task_id))
    assert result.startswith("blocked:")
    assert "capability matrix" in result
    assert store.task(task_id)["status"] == "blocked"
    assert not marker.exists()
    assert not any(item["source"] == "verification" for item in store.evidence(task_id))

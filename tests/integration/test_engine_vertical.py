from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from agent_center.config import AppConfiguration
from agent_center.domain import (
    Criterion,
    Delegate,
    EvidenceClaim,
    FinishTask,
    PlanStep,
    UpdatePlan,
    Verify,
)
from agent_center.engine import TaskEngine
from agent_center.storage.database import Store


class ScriptedSupervisor:
    def __init__(self, store: Store, task_id: str, step: PlanStep, agent_id: str = "codex"):
        self.store = store
        self.task_id = task_id
        self.step = step
        self.agent_id = agent_id
        self.actions: list[Any] = [
            UpdatePlan(action="update_plan", reason_summary="synthetic plan", steps=[step]),
            Delegate(
                action="delegate",
                reason_summary="synthetic execution",
                agent_id=agent_id,
                step_id=step.id,
                objective=step.objective,
                acceptance_criteria=step.acceptance_criteria,
            ),
            Verify(
                action="verify",
                reason_summary="independent synthetic test",
                criterion_ids=[criterion.id for criterion in step.acceptance_criteria],
            ),
        ]

    async def decide(self, **_: Any) -> Any:
        if self.actions:
            return self.actions.pop(0)
        task = self.store.task(self.task_id)
        evidence = self.store.evidence(self.task_id, int(task["requirements_version"]))
        verifier = next(item for item in evidence if item["source"] == "verification")
        criterion_id = self.step.acceptance_criteria[0].id
        return FinishTask(
            action="finish_task",
            reason_summary="synthetic test output reviewed",
            summary="Synthetic implementation verified by a workspace test.",
            evidence_ids=[verifier["evidence_id"]],
            claims=[EvidenceClaim(evidence_id=verifier["evidence_id"], criterion_id=criterion_id)],
        )


def test_synthetic_supervisor_agent_verifier_vertical_slice(
    config: AppConfiguration, store: Store, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    tests = workspace / "tests"
    tests.mkdir(parents=True)
    (workspace / "answer.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    (tests / "test_answer.py").write_text(
        "from answer import answer\n\ndef test_answer():\n    assert answer() == 42\n",
        encoding="utf-8",
    )

    fake_cli = tmp_path / "fake-codex"
    fake_cli.write_text(
        f"#!{os.sys.executable}\n"
        "import json\n"
        "events = [\n"
        " {'type':'thread.started','thread_id':'synthetic'},\n"
        " {'type':'item.completed','item':{'type':'agent_message','text':'Synthetic output only.'}},\n"
        " {'type':'turn.completed'}\n"
        "]\n"
        "for event in events: print(json.dumps(event), flush=True)\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o700)

    codex_config = config.agents["codex"].model_copy(
        update={"executable": str(fake_cli), "allow_execution": True, "capability_status": "supported"}
    )
    deepseek_config = config.agents["deepseek"].model_copy(update={"allow_execution": False})
    config = config.model_copy(update={"agents": {"codex": codex_config, "deepseek": deepseek_config}})

    criterion = Criterion(
        id="answer-test",
        description="The test for the requested answer function passes.",
        verification_argv=["python", "-m", "pytest", "-q", "tests/test_answer.py"],
    )
    step = PlanStep(
        id="implement-answer", objective="Implement the small answer function.", acceptance_criteria=[criterion]
    )
    task_id, _ = TaskEngine(config, store, model_client=None).create_task(
        "Implement a small answer function in this workspace.", workspace
    )
    engine = TaskEngine(
        config,
        store,
        model_client=ScriptedSupervisor(store, task_id, step),
    )
    result = asyncio.run(engine.run_task(task_id))
    assert result == "Synthetic implementation verified by a workspace test."
    task = store.task(task_id)
    assert task["status"] == "succeeded"
    assert task["agent_calls"] == 1
    assert task["supervisor_calls"] == 4
    assert (workspace / "answer.py").read_text(encoding="utf-8").startswith("def answer")
    verifier_evidence = next(item for item in store.evidence(task_id) if item["source"] == "verification")
    assert verifier_evidence["workspace_sha256"]
    assert engine.verifier.is_fresh(task_id, [verifier_evidence["evidence_id"]])
    (workspace / "answer.py").write_text("def answer():\n    return 0\n", encoding="utf-8")
    assert not engine.verifier.is_fresh(task_id, [verifier_evidence["evidence_id"]])
    assert all(run["status"] == "completed" for run in store.runs(task_id))

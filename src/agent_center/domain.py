from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TaskStatus(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    RUNNING = "running"
    VERIFYING = "verifying"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    BLOCKED = "blocked"
    INTERRUPTED = "interrupted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunStatus(StrEnum):
    PREPARED = "prepared"
    STARTING = "starting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class Criterion(StrictModel):
    id: str = Field(min_length=1, max_length=80)
    description: ShortText
    verification_argv: list[str] = Field(min_length=1, max_length=32)


class DirectAnswer(StrictModel):
    action: Literal["answer"]
    reason_summary: ShortText
    answer: ShortText


class ExecutionRequest(StrictModel):
    action: Literal["request_execution"]
    reason_summary: ShortText


class PlanStep(StrictModel):
    id: str = Field(min_length=1, max_length=80)
    objective: ShortText
    acceptance_criteria: list[Criterion] = Field(min_length=1, max_length=20)


class UpdatePlan(StrictModel):
    action: Literal["update_plan"]
    reason_summary: ShortText
    steps: list[PlanStep] = Field(min_length=1, max_length=20)


class Delegate(StrictModel):
    action: Literal["delegate"]
    reason_summary: ShortText
    agent_id: Literal["codex", "deepseek"]
    step_id: str = Field(min_length=1, max_length=80)
    objective: ShortText
    acceptance_criteria: list[Criterion] = Field(min_length=1, max_length=20)


class Verify(StrictModel):
    action: Literal["verify"]
    reason_summary: ShortText
    criterion_ids: list[str] = Field(min_length=1, max_length=20)


class Inspect(StrictModel):
    action: Literal["inspect"]
    reason_summary: ShortText
    path: str = Field(min_length=1, max_length=1000)
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=120, ge=1, le=300)


class EvidenceClaim(StrictModel):
    evidence_id: str = Field(min_length=1, max_length=100)
    criterion_id: str = Field(min_length=1, max_length=80)


class FinishTask(StrictModel):
    action: Literal["finish_task"]
    reason_summary: ShortText
    summary: ShortText
    evidence_ids: list[str] = Field(min_length=1, max_length=50)
    claims: list[EvidenceClaim] = Field(min_length=1, max_length=100)


class RequestApproval(StrictModel):
    action: Literal["request_approval"]
    reason_summary: ShortText
    operation: ShortText
    target_path: str = Field(min_length=1, max_length=1000)
    risk: ShortText


SupervisorAction = Annotated[
    DirectAnswer | UpdatePlan | Delegate | Verify | Inspect | FinishTask | RequestApproval,
    Field(discriminator="action"),
]


class Evidence(StrictModel):
    evidence_id: str
    requirements_version: int = Field(ge=1)
    run_id: str | None = None
    source: Literal["supervisor", "agent_event", "filesystem"]
    summary: ShortText
    path: str | None = None
    sha256: str | None = None
    complete: bool = True

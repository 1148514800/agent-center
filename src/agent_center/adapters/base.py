from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from agent_center.config import AgentConfig, ProfileConfig
from agent_center.runtime.process import ProcessResult


@dataclass(frozen=True)
class AgentRequest:
    objective: str
    task_id: str
    run_id: str
    requirements_version: int
    workspace: Path
    profile: ProfileConfig
    config: AgentConfig
    context: str
    timeout_seconds: int
    log_dir: Path
    approved_scopes: tuple[str, ...] = ()
    verification_command: tuple[str, ...] = ()


@dataclass(frozen=True)
class NormalizedEvent:
    kind: str
    summary: str
    raw: dict[str, Any]
    terminal: bool = False
    failure: bool = False


@dataclass(frozen=True)
class CommandExecution:
    command: str
    cwd: str | None
    exit_code: int | None
    status: str
    output: str | None
    event_id: str
    finished: bool


@dataclass(frozen=True)
class AgentRunResult:
    process: ProcessResult
    events: tuple[NormalizedEvent, ...]
    completed: bool
    error: str | None
    final_text: str
    commands: tuple[CommandExecution, ...] = ()
    approval_requests: tuple[str, ...] = ()


@dataclass(frozen=True)
class LaunchSpec:
    executable: str
    argv: tuple[str, ...]
    environment: dict[str, str]
    stdin: bytes
    parser: str


class AgentAdapter(Protocol):
    def build_launch(self, request: AgentRequest) -> LaunchSpec: ...

    def parse(self, request: AgentRequest, process: ProcessResult) -> AgentRunResult: ...


def parse_jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    events: list[dict[str, Any]] = []
    with path.open("rb") as stream:
        for index, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            if not line.endswith(b"\n"):
                raise ValueError(f"Truncated JSONL event on line {index}")
            try:
                obj = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"Malformed JSONL event on line {index}") from exc
            if not isinstance(obj, dict):
                raise ValueError(f"Non-object JSONL event on line {index}")
            events.append(obj)
    return tuple(events)


def verification_prompt(request: AgentRequest) -> str:
    if not request.verification_command:
        return ""
    import shlex

    return (
        "\nIndependent verification Run. Execute exactly this saved Plan command once, using the "
        "foreground command tool and the explicit absolute workdir below. Do not edit files, add "
        "commands, retry with broader permissions, use background jobs, or substitute tools. "
        "If denied, stop and report the permission request.\n"
        f"workdir: {request.workspace}\ncommand: {shlex.join(request.verification_command)}\n"
    )

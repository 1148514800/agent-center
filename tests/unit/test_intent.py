from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from agent_center.config import AppConfiguration
from agent_center.domain import TaskStatus
from agent_center.engine import TaskEngine
from agent_center.storage.database import Store
from agent_center.supervisor import ModelClient


@pytest.mark.parametrize("goal", [
    "你好", "What is SQLite?", "Please add JSON output to the tool.",
    "修改这个页面并添加测试", "Explain SQLite and then implement a database tool.",
])
@pytest.mark.parametrize("followups", [[], ["帮我改代码"], ["帮我改代码", "谢谢，先回答问题"]])
async def test_supervisor_can_answer_any_wording_without_cli(
    config: AppConfiguration, store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    goal: str, followups: list[str],
) -> None:
    monkeypatch.setenv("AGENT_CENTER_BASE_URL", "https://synthetic.invalid/v1")
    monkeypatch.setenv("AGENT_CENTER_API_KEY", "SYNTHETIC_ROUTING_TEST_VALUE")
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "模型选择直接回答"}}]})

    engine = TaskEngine(config, store, model_client=ModelClient(config, transport=httpx.MockTransport(respond)))
    engine.runner.run = AsyncMock(side_effect=AssertionError("No CLI should be launched"))
    task_id, _ = engine.create_task(goal, tmp_path)
    for followup in followups:
        store.add_message(task_id, followup)
        store.apply_pending_messages(task_id)
    assert await engine.run_task(task_id) == "模型选择直接回答"
    task = store.task(task_id)
    assert task["status"] == TaskStatus.SUCCEEDED
    assert task["supervisor_calls"] == len(requests) == 1
    assert task["agent_calls"] == 0
    assert not store.runs(task_id)
    assert not any(event["type"] == "direct_answer_rejected" for event in store.events(task_id))
    engine.runner.run.assert_not_awaited()
    assert requests[0]["tool_choice"] == "auto"
    assert "delegate" in {tool["function"]["name"] for tool in requests[0]["tools"]}
    context = json.loads(requests[0]["messages"][1]["content"])
    assert context["requirements"] == [goal, *followups]


@pytest.mark.parametrize("goal", ["你好", "What is SQLite?"])
async def test_supervisor_can_choose_cli_for_any_wording(
    config: AppConfiguration, store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, goal: str,
) -> None:
    monkeypatch.setenv("AGENT_CENTER_BASE_URL", "https://synthetic.invalid/v1")
    monkeypatch.setenv("AGENT_CENTER_API_KEY", "SYNTHETIC_ROUTING_TEST_VALUE")
    step = {"id": "s", "objective": "Inspect the local implementation", "acceptance_criteria": [
        {"id": "c", "description": "Check local behavior",
         "verification_argv": ["python", "-m", "pytest", "-q", "tests/test_local.py"]},
    ]}
    responses = [
        ("update_plan", {"steps": [step], "reason_summary": "Local inspection needed"}),
        ("delegate", {"agent_id": "codex", "step_id": step["id"], "objective": step["objective"],
                      "acceptance_criteria": step["acceptance_criteria"], "reason_summary": "Use local CLI"}),
    ]
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        name, arguments = responses.pop(0)
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [
            {"type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}},
        ]}}]})

    engine = TaskEngine(config, store, model_client=ModelClient(config, transport=httpx.MockTransport(respond)))
    pause = asyncio.Event()

    async def delegate(*_: Any, **__: Any) -> tuple[list[str], str, str, bool]:
        pause.set()
        return [], "Synthetic execution", "synthetic-run", True

    dispatch = AsyncMock(side_effect=delegate)
    monkeypatch.setattr(engine, "_delegate", dispatch)
    task_id, _ = engine.create_task(goal, tmp_path)
    await engine.run_task(task_id, pause_event=pause)
    dispatch.assert_awaited_once()
    assert dispatch.call_args.args[1].agent_id == "codex"
    assert store.task(task_id)["status"] == TaskStatus.PAUSED
    assert store.task(task_id)["supervisor_calls"] == len(requests) == 2
    assert [json.loads(event["payload"])["action"] for event in store.events(task_id)
            if event["type"] == "supervisor_action"] == ["update_plan", "delegate"]

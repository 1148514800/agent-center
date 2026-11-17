from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agent_center.config import AppConfiguration
from agent_center.domain import DirectAnswer
from agent_center.supervisor import TOOL_DEFINITIONS, ModelClient, ModelError


def _config(config: AppConfiguration, monkeypatch: pytest.MonkeyPatch) -> AppConfiguration:
    monkeypatch.setenv("AGENT_CENTER_BASE_URL", "https://local.example/v1")
    monkeypatch.setenv("AGENT_CENTER_API_KEY", "SYNTHETIC_KEY_NOT_A_REAL_CREDENTIAL")
    return config


def test_tool_schemas_are_valid_strict_objects() -> None:
    assert {tool["function"]["name"] for tool in TOOL_DEFINITIONS} == {
        "update_plan",
        "delegate",
        "verify",
        "inspect",
        "request_approval",
        "finish_task",
    }
    for tool in TOOL_DEFINITIONS:
        assert tool["function"]["parameters"]["additionalProperties"] is False


def test_model_api_direct_answer_with_mock_transport(config: AppConfiguration, monkeypatch: pytest.MonkeyPatch) -> None:
    _config(config, monkeypatch)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Synthetic answer"}}]})

    model = ModelClient(config, transport=httpx.MockTransport(handler))
    action = asyncio.run(model.decide(system="system", user="question", allow_execution=True))
    assert isinstance(action, DirectAnswer)
    assert action.answer == "Synthetic answer"
    assert requests[0].url.path == "/v1/chat/completions"
    payload = json.loads(requests[0].content)
    assert payload["model"] == "test-model"
    assert "tools" in payload
    assert requests[0].headers["authorization"] == "Bearer SYNTHETIC_KEY_NOT_A_REAL_CREDENTIAL"


def test_model_api_rejects_unknown_agent_before_dispatch(
    config: AppConfiguration, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(config, monkeypatch)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "delegate",
                                        "arguments": json.dumps(
                                            {
                                                "agent_id": "unknown",
                                                "step_id": "s1",
                                                "objective": "x",
                                                "acceptance_criteria": [
                                                    {
                                                        "id": "c",
                                                        "description": "x",
                                                        "verification_argv": [
                                                            "pytest",
                                                            "-q",
                                                            "tests/test_x.py",
                                                        ],
                                                    }
                                                ],
                                                "reason_summary": "x",
                                            }
                                        ),
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
        )

    model = ModelClient(config, transport=httpx.MockTransport(handler))
    with pytest.raises(ModelError, match="invalid action"):
        asyncio.run(model.decide(system="s", user="u", allow_execution=True))
    assert calls == 1


def test_model_api_rejects_multiple_tools(config: AppConfiguration, monkeypatch: pytest.MonkeyPatch) -> None:
    _config(config, monkeypatch)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {"function": {"name": "inspect", "arguments": "{}"}},
                                {"function": {"name": "inspect", "arguments": "{}"}},
                            ]
                        }
                    }
                ]
            },
        )

    model = ModelClient(config, transport=httpx.MockTransport(handler))
    with pytest.raises(ModelError, match="Multiple"):
        asyncio.run(model.decide(system="s", user="u", allow_execution=True))


def test_supervisor_model_options_reach_transport(config: AppConfiguration, monkeypatch: pytest.MonkeyPatch) -> None:
    _config(config, monkeypatch)
    config = config.model_copy(update={"supervisor": config.supervisor.model_copy(update={
        "model": "selected-model", "reasoning_effort": "high", "max_output_tokens": 2048,
        "request_timeout_seconds": 30,
    })})

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] == "selected-model"
        assert payload["reasoning_effort"] == "high"
        assert payload["max_tokens"] == 2048
        assert request.extensions["timeout"]["read"] == 30
        return httpx.Response(200, json={"choices": [{"message": {"content": "synthetic"}}]})

    asyncio.run(ModelClient(config, transport=httpx.MockTransport(handler)).decide(
        system="system", user="question", allow_execution=True
    ))

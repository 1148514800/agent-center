from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agent_center.config import AppConfiguration
from agent_center.domain import DirectAnswer
from agent_center.supervisor import TOOL_DEFINITIONS, ModelClient, ModelError, MultipleToolCallsError


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
    assert payload["parallel_tool_calls"] is False
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
    with pytest.raises(MultipleToolCallsError, match="Multiple") as error:
        asyncio.run(model.decide(system="s", user="u", allow_execution=True))
    assert error.value.tool_names == ["inspect", "inspect"]
    assert error.value.count == 2


def test_read_only_question_sends_no_tools(config: AppConfiguration, monkeypatch: pytest.MonkeyPatch) -> None:
    _config(config, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert "tools" not in payload
        assert "parallel_tool_calls" not in payload
        return httpx.Response(200, json={"choices": [{"message": {"content": "Synthetic answer"}}]})

    model = ModelClient(config, transport=httpx.MockTransport(handler))
    assert asyncio.run(model.answer_question(system="s", user="u")) == "Synthetic answer"


def test_multi_tool_diagnostics_keep_unknown_names_and_redact_credentials(
    config: AppConfiguration, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(config, monkeypatch)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [
            {"function": {"name": "unsupported_tool", "arguments": "PRIVATE_ARGUMENT_CANARY"}},
            {"function": {"name": "SYNTHETIC_KEY_NOT_A_REAL_CREDENTIAL", "arguments": "{}"}},
        ]}}]})

    model = ModelClient(config, transport=httpx.MockTransport(handler))
    with pytest.raises(MultipleToolCallsError) as error:
        asyncio.run(model.decide(system="s", user="u", allow_execution=True))
    assert error.value.tool_names[0] == "unsupported_tool"
    assert error.value.count == 2
    assert "SYNTHETIC_KEY_NOT_A_REAL_CREDENTIAL" not in str(error.value)
    assert "PRIVATE_ARGUMENT_CANARY" not in str(error.value)


def test_supervisor_model_options_reach_transport(config: AppConfiguration, monkeypatch: pytest.MonkeyPatch) -> None:
    _config(config, monkeypatch)
    config = config.model_copy(update={"supervisor": config.supervisor.model_copy(update={
        "model": "gpt-5", "reasoning_effort": "high", "max_output_tokens": 2048,
        "request_timeout_seconds": 30,
    })})

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] == "gpt-5"
        assert payload["reasoning_effort"] == "high"
        assert payload["max_completion_tokens"] == 2048
        assert "temperature" not in payload
        assert request.extensions["timeout"]["read"] == 30
        return httpx.Response(200, json={"choices": [{"message": {"content": "synthetic"}}]})

    asyncio.run(ModelClient(config, transport=httpx.MockTransport(handler)).decide(
        system="system", user="question", allow_execution=True
    ))

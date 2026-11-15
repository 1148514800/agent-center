from __future__ import annotations

import json
from typing import Any, cast

import httpx
from pydantic import TypeAdapter, ValidationError

from agent_center.config import AppConfiguration
from agent_center.domain import DirectAnswer, SupervisorAction
from agent_center.storage.redaction import redact_text


def _text(max_length: int, *, min_length: int = 1) -> dict[str, Any]:
    return {"type": "string", "minLength": min_length, "maxLength": max_length}


def _array(items: dict[str, Any], *, maximum: int, minimum: int = 1) -> dict[str, Any]:
    return {"type": "array", "minItems": minimum, "maxItems": maximum, "items": items}


def _criterion_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "description", "verification_argv"],
        "properties": {
            "id": _text(80),
            "description": _text(500),
            "verification_argv": _array(_text(500), maximum=32),
        },
    }


def _step_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "objective", "acceptance_criteria"],
        "properties": {
            "id": _text(80),
            "objective": _text(4000),
            "acceptance_criteria": _array(_criterion_schema(), maximum=20),
        },
    }


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": required,
                "properties": properties,
            },
        },
    }


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _tool(
        "update_plan",
        "Set a plan with fixed Python pytest commands under workspace/tests for each criterion.",
        {"reason_summary": _text(500), "steps": _array(_step_schema(), maximum=20)},
        ["steps", "reason_summary"],
    ),
    _tool(
        "delegate",
        "Run one exact saved plan step with a configured local agent.",
        {
            "agent_id": {"type": "string", "enum": ["codex", "deepseek"]},
            "step_id": _text(80),
            "objective": _text(4000),
            "acceptance_criteria": _array(_criterion_schema(), maximum=20),
            "reason_summary": _text(500),
        },
        ["agent_id", "step_id", "objective", "acceptance_criteria", "reason_summary"],
    ),
    _tool(
        "verify",
        "Run the fixed, allowlisted verification command for saved plan criteria.",
        {"reason_summary": _text(500), "criterion_ids": _array(_text(80), maximum=20)},
        ["criterion_ids", "reason_summary"],
    ),
    _tool(
        "inspect",
        "Read a bounded text range inside the assigned workspace.",
        {
            "path": _text(1000),
            "reason_summary": _text(500),
            "start_line": {"type": "integer", "minimum": 1},
            "max_lines": {"type": "integer", "minimum": 1, "maximum": 300},
        },
        ["path", "reason_summary"],
    ),
    _tool(
        "request_approval",
        "Persist a workspace-contained approval request; this action never grants access.",
        {
            "operation": _text(500),
            "target_path": _text(1000),
            "risk": _text(1000),
            "reason_summary": _text(500),
        },
        ["operation", "target_path", "risk", "reason_summary"],
    ),
    _tool(
        "finish_task",
        "Finish only when every current plan criterion has current independent verification evidence.",
        {
            "summary": _text(4000),
            "reason_summary": _text(500),
            "evidence_ids": _array(_text(100), maximum=50),
            "claims": _array(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["evidence_id", "criterion_id"],
                    "properties": {"evidence_id": _text(100), "criterion_id": _text(80)},
                },
                maximum=100,
            ),
        },
        ["summary", "evidence_ids", "claims", "reason_summary"],
    ),
]


class ModelError(RuntimeError):
    pass


class ModelClient:
    def __init__(
        self,
        config: AppConfiguration,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        max_response_bytes: int = 1_000_000,
    ):
        self.config = config
        self.transport = transport
        self.max_response_bytes = max_response_bytes

    async def decide(self, *, system: str, user: str, allow_execution: bool) -> SupervisorAction:
        base_url, api_key, model = self.config.supervisor_endpoint()
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": self.config.supervisor.max_output_tokens,
            "temperature": 0,
        }
        if allow_execution:
            body["tools"] = TOOL_DEFINITIONS
            body["tool_choice"] = "auto"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        timeout = httpx.Timeout(self.config.supervisor.request_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, transport=self.transport) as client:
            try:
                endpoint = (
                    f"{base_url}/chat/completions" if base_url.endswith("/v1") else f"{base_url}/v1/chat/completions"
                )
                async with client.stream("POST", endpoint, headers=headers, json=body) as response:
                    if response.status_code >= 400:
                        raise ModelError(f"Supervisor API returned HTTP {response.status_code}")
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self.max_response_bytes:
                            raise ModelError("Supervisor response exceeded the configured byte limit")
                        chunks.append(chunk)
                    payload = json.loads(b"".join(chunks))
            except ModelError:
                raise
            except (httpx.HTTPError, json.JSONDecodeError) as exc:
                raise ModelError(f"Supervisor transport/protocol error ({type(exc).__name__})") from exc
        try:
            message = payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError("Supervisor response has an unsupported shape") from exc
        tool_calls = message.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            raise ModelError("Supervisor tool_calls must be an array")
        if len(tool_calls) > 1:
            raise ModelError("Multiple tool calls in one response are not supported")
        if tool_calls:
            tool_call = tool_calls[0]
            function = tool_call.get("function", {})
            name = function.get("name")
            try:
                arguments = json.loads(function.get("arguments", ""))
            except (json.JSONDecodeError, TypeError) as exc:
                raise ModelError("Supervisor returned malformed tool arguments") from exc
            if not isinstance(arguments, dict):
                raise ModelError("Supervisor tool arguments must be an object")
            arguments = json.loads(redact_text(json.dumps(arguments), [api_key]))
            arguments["action"] = name
            try:
                adapter = TypeAdapter(cast(Any, SupervisorAction))
                return cast(SupervisorAction, adapter.validate_python(arguments))
            except (ValidationError, ValueError, TypeError) as exc:
                raise ModelError(f"Supervisor proposed an invalid action: {type(exc).__name__}") from exc
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ModelError("Supervisor returned neither a tool call nor a text answer")
        return DirectAnswer(
            action="answer",
            reason_summary="The supervisor chose to respond directly.",
            answer=redact_text(content[:4000], [api_key]),
        )

    async def answer_question(self, *, system: str, user: str) -> str:
        action = await self.decide(system=system, user=user, allow_execution=False)
        if not isinstance(action, DirectAnswer):
            raise ModelError("Read-only question response was not plain text")
        return action.answer

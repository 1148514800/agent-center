from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from agent_center.config import offline_default_configuration
from agent_center.models import codex_models, normalize_codex_models
from agent_center.setup import apply_registrations, register_agent


def test_native_catalog_uses_per_model_efforts() -> None:
    models = normalize_codex_models(
        [
            {
                "id": "a",
                "model": "wire-a",
                "displayName": "A",
                "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
            },
            {"id": "b", "supportedReasoningEfforts": []},
            {"id": "hidden", "hidden": True},
        ]
    )
    assert models[0]["id"] == "wire-a" and models[0]["efforts"] == ["high"]
    assert models[1]["efforts"] == []
    assert len(models) == 2


@pytest.mark.asyncio
async def test_stdio_model_discovery_never_starts_thread(tmp_path: Path) -> None:
    executable = tmp_path / "fake-codex"
    script = """import sys,json
for line in sys.stdin:
 r=json.loads(line); method=r['method']
 if method=='initialized':continue
 assert method in {'initialize','model/list'}
 row={'id':'model-test','supportedReasoningEfforts':[{'reasoningEffort':'low'}]}
 result={} if method=='initialize' else {'data':[row]}
 print(json.dumps({'id':r['id'],'result':result}),flush=True)
"""
    executable.write_text(f"#!{sys.executable}\n" + script)
    executable.chmod(0o700)
    result = await asyncio.wait_for(codex_models(str(executable)), 5)
    assert result["models"][0]["id"] == "model-test"
    assert result["models"][0]["efforts"] == ["low"]


def test_effort_persisted_without_enabling_execution(tmp_path: Path) -> None:
    executable = tmp_path / "cli"
    executable.write_text("#!/bin/sh\nexit 0")
    executable.chmod(0o700)
    path = tmp_path / "config.toml"
    register_agent(
        offline_default_configuration(),
        path,
        {
            "agent_id": "codex",
            "model": "model-a",
            "executable": str(executable),
            "reasoning_effort": "high",
        },
    )
    config = apply_registrations(offline_default_configuration(), path)
    assert config.profiles["registered_codex"].reasoning_effort == "high"
    assert not config.agents["codex"].allow_execution


def test_codex_catalog_deduplicates_wire_ids_without_collapsing_distinct_models() -> None:
    rows = [
        {"id": "alias", "model": "wire", "displayName": "Model (wire)", "isDefault": False},
        {"id": "wire", "displayName": "Model (wire)", "isDefault": True,
         "supportedReasoningEfforts": [{"reasoningEffort": "high"}]},
        {"model": "other", "displayName": "Model (wire)"},
        {"id": "hidden-alias", "model": "wire", "hidden": True},
        {"id": "", "model": 123},
    ]
    models = normalize_codex_models(rows)
    assert [m["id"] for m in models] == ["wire", "other"]
    assert models[0]["name"] == models[1]["name"] == "Model (wire)"
    assert models[0]["default"] and models[0]["efforts"] == ["high"]

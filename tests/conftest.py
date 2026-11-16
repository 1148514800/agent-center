from __future__ import annotations

from pathlib import Path

import pytest

from agent_center.config import AppConfiguration
from agent_center.storage.database import Store


@pytest.fixture
def config(tmp_path: Path) -> AppConfiguration:
    return AppConfiguration.model_validate(
        {
            "schema_version": 1,
            "app": {
                "data_dir": str(tmp_path / "data"),
                "default_workspace_root": str(tmp_path / "workspaces"),
            },
            "supervisor": {"protocol": "openai_chat_completions", "model": "test-model"},
            "agents": {
                "codex": {
                    "adapter": "codex_exec",
                    "executable": "/tmp/fake-codex",
                    "description": "fake",
                    "default_profile": "codex",
                    "capability_status": "supported",
                    "allow_execution": True,
                },
                "deepseek": {
                    "adapter": "deepseek_headless",
                    "executable": "/tmp/fake-dsh",
                    "description": "fake",
                    "default_profile": "deepseek",
                    "capability_status": "supported",
                    "allow_execution": True,
                },
            },
            "profiles": {
                "codex": {
                    "agent_id": "codex",
                    "model": "codex-test",
                    "permission_mode": "read-only",
                },
                "deepseek": {
                    "agent_id": "deepseek",
                    "provider": "deepseek-official",
                    "model": "dsh-test",
                    "permission_mode": "read-only",
                },
            },
        }
    )


@pytest.fixture
def store(config: AppConfiguration) -> Store:
    instance = Store(Path(config.app.data_dir))
    yield instance
    instance.close()


@pytest.fixture
def verified_config(config: AppConfiguration, tmp_path: Path) -> AppConfiguration:
    """Synthetic attestations only; never used by real-agent configuration."""
    from agent_center.capabilities import AgentCapabilities, installation_identity

    agents = {}
    for agent_id, agent in config.agents.items():
        executable = tmp_path / f"fake-{agent_id}"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        agent = agent.model_copy(update={"executable": str(executable)})
        profile = config.profiles[agent.default_profile]
        values = {
            name: "SUPPORTED" for name in AgentCapabilities.model_fields if name.endswith(("supported", "verified"))
        }
        values.update(
            installation_identity=installation_identity(agent, profile), evidence_reference="synthetic-U/I-only"
        )
        agents[agent_id] = agent.model_copy(update={"capabilities": AgentCapabilities.model_validate(values)})
    return config.model_copy(update={"agents": agents})

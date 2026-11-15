from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_center.config import AppConfiguration, load_config


def test_example_config_is_strictly_loadable() -> None:
    config = load_config(Path("config.example.toml"))
    assert config.policy.allow_full_access is False
    assert config.policy.allow_external_publish is False
    assert config.agents["codex"].allow_execution is False


def test_unknown_config_field_rejected(config: AppConfiguration) -> None:
    raw = config.model_dump()
    raw["unsafe_shell"] = True
    with pytest.raises(ValidationError):
        AppConfiguration.model_validate(raw)


def test_model_placeholder_rejected_for_live_api(config: AppConfiguration, monkeypatch: pytest.MonkeyPatch) -> None:
    config = config.model_copy(
        update={"supervisor": config.supervisor.model_copy(update={"model": "REPLACE_WITH_MODEL"})}
    )
    monkeypatch.setenv("AGENT_CENTER_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("AGENT_CENTER_API_KEY", "synthetic-secret")
    with pytest.raises(ValueError, match="placeholder"):
        config.supervisor_endpoint()


def test_https_required_outside_loopback(config: AppConfiguration, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_CENTER_BASE_URL", "http://example.com")
    monkeypatch.setenv("AGENT_CENTER_API_KEY", "synthetic-secret")
    with pytest.raises(ValueError, match="HTTPS"):
        config.supervisor_endpoint()

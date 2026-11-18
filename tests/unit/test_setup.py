from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_center.config import AppConfiguration, load_config, offline_default_configuration
from agent_center.engine import TaskEngine
from agent_center.setup import apply_registrations, browse_directory, register_agent, registrations_path
from agent_center.storage.database import Store
from agent_center.web_server import WebApplication


@pytest.fixture
def executable(tmp_path: Path) -> Path:
    path = tmp_path / "fake agent"
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o700)
    return path


def test_registration_preserves_toml_and_invalidates_native_capabilities(
    executable: Path,
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes(Path("config.example.toml").read_bytes())
    original = path.read_bytes()
    config = register_agent(
        load_config(path),
        path,
        {
            "agent_id": "codex",
            "executable": str(executable),
            "model": "local-model",
        },
    )
    assert path.read_bytes() == original
    assert registrations_path(path).stat().st_mode & 0o777 == 0o600
    restored = load_config(path)
    assert restored == config
    assert not restored.agents["codex"].allow_execution
    assert restored.agents["codex"].capabilities.installation_identity == ""
    with pytest.raises(ValueError, match="--replace"):
        register_agent(restored, path, {"agent_id": "codex", "executable": str(executable), "model": "new-model"})
    register_agent(
        restored, path, {"agent_id": "codex", "executable": str(executable), "model": "new-model"}, replace=True
    )
    assert load_config(path).profiles["registered_codex"].model == "new-model"


def test_deepseek_registration_without_base_toml(executable: Path, tmp_path: Path) -> None:
    package = tmp_path / "dsh"
    (package / "lib").mkdir(parents=True)
    (package / "lib/bin.js").write_text("// synthetic")
    path = tmp_path / "not-created.toml"
    register_agent(
        offline_default_configuration(),
        path,
        {
            "agent_id": "deepseek",
            "executable": str(executable),
            "model": "test-model",
            "provider": "deepseek-official",
            "cli_install_dir": str(package),
        },
    )
    assert not path.exists()
    config = apply_registrations(offline_default_configuration(), path)
    assert config.agents["deepseek"].executable == str(executable)
    assert not config.agents["deepseek"].allow_execution


def test_registration_rejects_invalid_executable_and_permission(tmp_path: Path, executable: Path) -> None:
    for values in (
        {"executable": "/missing"},
        {"executable": str(executable), "permission_mode": "danger-full-access"},
    ):
        with pytest.raises(ValueError):
            register_agent(
                offline_default_configuration(),
                tmp_path / "config.toml",
                {
                    "agent_id": "codex",
                    "model": "test",
                    **values,
                },
            )
    assert not registrations_path(tmp_path / "config.toml").exists()


def test_directory_browser_returns_paths_without_reading_files(tmp_path: Path, executable: Path) -> None:
    (tmp_path / "project").mkdir()
    (tmp_path / "secret.txt").write_text("not returned")
    assert [e["name"] for e in browse_directory(str(tmp_path))["entries"]] == ["project"]
    entries = browse_directory(str(tmp_path), files=True)["entries"]
    assert any(e["path"] == str(executable) and not e["directory"] for e in entries)
    assert "not returned" not in json.dumps(entries)
    with pytest.raises(ValueError):
        browse_directory(str(executable))


@pytest.mark.asyncio
async def test_web_registration_and_active_task_guard(
    config: AppConfiguration, store: Store, executable: Path, tmp_path: Path
) -> None:
    path = tmp_path / "config.toml"
    app = WebApplication(TaskEngine(config, store), store, path)
    values = {"agent_id": "codex", "executable": str(executable), "model": "new", "replace": True}
    assert (await app.api("POST", "/api/agents", values))["saved"]
    assert app.engine.config.profiles["registered_codex"].model == "new"
    task_id, _ = app.engine.create_task("Implement", tmp_path)
    store._connection.execute("UPDATE tasks SET status='running' WHERE task_id=?", (task_id,))
    with pytest.raises(ValueError, match="当前任务"):
        await app.api("POST", "/api/agents", values)
    assert not store.runs(task_id)


def test_noninteractive_cli_registers_and_lists(executable: Path, tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    env = {**os.environ, "AGENT_CENTER_CONFIG": str(path)}
    registered = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_center.cli",
            "--data-dir",
            str(tmp_path / "state"),
            "agent",
            "add",
            "codex",
            "--executable",
            str(executable),
            "--model",
            "synthetic-model",
        ],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert registered.returncode == 0, registered.stderr
    listed = subprocess.run(
        [sys.executable, "-m", "agent_center.cli", "agents"], env=env, capture_output=True, text=True, timeout=10
    )
    assert listed.returncode == 0
    assert "synthetic-model" in listed.stdout and "enabled=False" in listed.stdout


def test_custom_connection_persists_without_secrets(executable: Path, tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config = register_agent(
        offline_default_configuration(),
        path,
        {
            "agent_id": "codex",
            "model": "my-model",
            "executable": str(executable),
            "base_url": "https://gateway.example/v1/",
            "api_key_env": "CODEX_API_KEY",
        },
    )
    profile = config.profiles["registered_codex"]
    assert profile.base_url == "https://gateway.example/v1"
    assert profile.api_key_env == "CODEX_API_KEY"
    assert apply_registrations(offline_default_configuration(), path) == config
    with pytest.raises(ValueError):
        register_agent(
            config,
            path,
            {
                "agent_id": "codex",
                "model": "m",
                "executable": str(executable),
                "base_url": "https://secret@example.com/v1",
            },
            replace=True,
        )


def test_directory_browser_can_show_hidden_directories(tmp_path: Path) -> None:
    (tmp_path / ".local").mkdir()
    assert not browse_directory(str(tmp_path))["entries"]
    assert browse_directory(str(tmp_path), hidden=True)["entries"][0]["name"] == ".local"


@pytest.mark.asyncio
async def test_web_session_key_never_persisted_or_exposed(
    config: AppConfiguration, store: Store, executable: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    # Restore the process environment after the test, including the API's memory-only assignment.
    monkeypatch.setenv("CODEX_API_KEY", "")
    path = tmp_path / "config.toml"
    app = WebApplication(TaskEngine(config, store), store, path)
    await app.api(
        "POST",
        "/api/agents",
        {
            "agent_id": "codex",
            "model": "test",
            "executable": str(executable),
            "replace": True,
            "base_url": "https://gateway.example/v1",
            "api_key_env": "CODEX_API_KEY",
            "api_key": "session-only-secret",
        },
    )
    assert os.environ["CODEX_API_KEY"] == "session-only-secret"
    assert "session-only-secret" not in registrations_path(path).read_text()
    assert "session-only-secret" not in json.dumps(await app.api("GET", "/api/state", {}))


def test_subscription_registration_allows_cli_default_model(executable: Path, tmp_path: Path) -> None:
    config = register_agent(
        offline_default_configuration(),
        tmp_path / "config.toml",
        {
            "agent_id": "codex",
            "executable": str(executable),
            "model": "",
        },
    )
    profile = config.profiles["registered_codex"]
    assert profile.model == profile.base_url == profile.api_key_env == ""
    assert not config.agents["codex"].allow_execution


@pytest.mark.asyncio
async def test_idle_initial_setup_can_be_repaired_but_unresolved_run_blocks_edits(
    config: AppConfiguration, store: Store, executable: Path, tmp_path: Path
) -> None:
    from agent_center.domain import TaskStatus

    app = WebApplication(TaskEngine(config, store), store, tmp_path / "config.toml")
    task_id, _ = app.engine.create_task("Implement", tmp_path)
    store.set_status(task_id, TaskStatus.BLOCKED, error="Supervisor missing")
    values = {"agent_id": "codex", "executable": str(executable), "model": "new", "replace": True}
    assert (await app.api("POST", "/api/agents", values))["saved"]
    store.set_status(task_id, TaskStatus.PLANNING)
    store.update_plan(task_id, [{"id": "s", "objective": "synthetic", "acceptance_criteria": [
        {"id": "c", "description": "synthetic", "verification_argv": []}
    ]}], 1)
    run_id = store.create_run(task_id, "codex", "synthetic", {}, tmp_path / "log", {}, 1)
    store.mark_run(run_id, "unknown")
    store.set_status(task_id, TaskStatus.CANCELLED)
    with pytest.raises(ValueError, match=run_id):
        await app.api("POST", "/api/agents", values)
    await app.api("POST", f"/api/tasks/{task_id}/reconcile", {"confirmation": task_id, "reviewed": True})
    assert (await app.api("POST", "/api/agents", values))["saved"]


@pytest.mark.asyncio
async def test_paused_task_with_history_allows_new_agent_but_not_replacement(
    config: AppConfiguration, store: Store, executable: Path, tmp_path: Path
) -> None:
    from agent_center.domain import TaskStatus

    raw = config.model_dump(mode="json")
    raw["agents"].pop("deepseek")
    config = AppConfiguration.model_validate(raw)
    app = WebApplication(TaskEngine(config, store), store, tmp_path / "config.toml")
    task_id, _ = app.engine.create_task("Implement", tmp_path)
    store.set_status(task_id, TaskStatus.PLANNING)
    store.update_plan(task_id, [{"id": "s", "objective": "synthetic", "acceptance_criteria": [
        {"id": "c", "description": "synthetic", "verification_argv": []}
    ]}], 1)
    run_id = store.create_run(task_id, "codex", "synthetic", {}, tmp_path / "log", {}, 1)
    store.mark_run(run_id, "completed", event_complete=True)
    store.set_status(task_id, TaskStatus.PAUSED)
    store.settle_active_period(task_id)
    package = tmp_path / "dsh"
    (package / "lib").mkdir(parents=True)
    (package / "lib/bin.js").write_text("// synthetic")
    values = {"agent_id": "deepseek", "executable": str(executable), "model": "new",
              "provider": "synthetic", "cli_install_dir": str(package)}
    assert (await app.api("POST", "/api/agents", values))["saved"]
    with pytest.raises(ValueError, match=task_id):
        await app.api("POST", "/api/agents", {**values, "replace": True})
    assert store.runs(task_id)[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_supervisor_settings_persist_private_connection_values(
    config: AppConfiguration, store: Store, executable: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_center.setup import register_supervisor
    from agent_center.storage.redaction import redact_value

    path = tmp_path / "config.toml"
    app = WebApplication(TaskEngine(config, store), store, path)
    monkeypatch.setenv("TEST_SUPERVISOR_ADDRESS", "")
    monkeypatch.setenv("TEST_SUPERVISOR_CREDENTIAL", "")
    values = {"protocol": "openai_chat_completions", "base_url_env": "TEST_SUPERVISOR_ADDRESS",
              "api_key_env": "TEST_SUPERVISOR_CREDENTIAL", "model": "gpt-5",
              "reasoning_effort": "high", "max_output_tokens": 2048, "request_timeout_seconds": 60}
    assert (await app.api("POST", "/api/supervisor", {**values, "base_url": "https://synthetic.invalid/v1",
                                                     "api_key": "synthetic-session-value"}))["saved"]
    assert app.engine.model.config is app.engine.config is app.engine.verifier.config
    assert app.engine.config.supervisor_endpoint() == (
        "https://synthetic.invalid/v1", "synthetic-session-value", "gpt-5"
    )
    from agent_center.setup import supervisor_settings_path

    persisted = supervisor_settings_path(path).read_text()
    assert "synthetic-session-value" in persisted and "https://synthetic.invalid" in persisted
    assert not path.exists()
    assert supervisor_settings_path(path).stat().st_mode & 0o777 == 0o600
    state = redact_value(await app.api("GET", "/api/state", {}))
    assert state["supervisor_settings"]["key_variable"] == "TEST_SUPERVISOR_CREDENTIAL"
    assert state["supervisor_settings"]["key_configured"]
    assert "synthetic-session-value" not in json.dumps(state)
    # Preserve existing agent rows when saving the supervisor, and vice versa.
    await app.api("POST", "/api/agents", {"agent_id": "codex", "executable": str(executable),
                                          "model": "synthetic-agent", "replace": True})
    register_supervisor(app.engine.config, path, {**values, "model": "o3"})
    restored = apply_registrations(config, path)
    assert restored.supervisor.model == "o3"
    assert restored.profiles["registered_codex"].model == "synthetic-agent"
    # Base TOML remains intact when present.
    path.write_bytes(Path("config.example.toml").read_bytes())
    original = path.read_bytes()
    assert load_config(path).supervisor.model == "o3"
    assert path.read_bytes() == original


@pytest.mark.asyncio
@pytest.mark.parametrize("key_variable", ["OPENAI_API_KEY", "DEEPSEEK_API_KEY", "PATH", "CODEX_HOME"])
async def test_supervisor_rejects_variables_that_could_reach_agents(
    config: AppConfiguration, store: Store, tmp_path: Path, key_variable: str
) -> None:
    app = WebApplication(TaskEngine(config, store), store, tmp_path / "config.toml")
    with pytest.raises(ValueError):
        await app.api("POST", "/api/supervisor", {"protocol": "openai_chat_completions", "model": "synthetic",
                                                 "api_key_env": key_variable})
    assert not registrations_path(tmp_path / "config.toml").exists()

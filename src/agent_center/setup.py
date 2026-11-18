"""Local agent registration; never edits native CLI configuration or grants permissions."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from agent_center.config import AppConfiguration, offline_default_configuration


def registrations_path(config_path: Path) -> Path:
    return config_path.expanduser().with_suffix(config_path.suffix + ".agents.json")


def apply_registrations(config: AppConfiguration, config_path: Path) -> AppConfiguration:
    path = registrations_path(config_path)
    raw = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(raw, dict) or set(raw) - {"codex", "deepseek", "supervisor"}:
        raise ValueError("Invalid registered agent data")
    if "supervisor" in raw:
        values = config.model_dump(mode="json")
        values["supervisor"] = raw["supervisor"]
        config = AppConfiguration.model_validate(values)
    for agent_id, values in raw.items():
        if agent_id == "supervisor":
            continue
        config = build_registration(config, agent_id=agent_id, **values)
    secret_path = supervisor_settings_path(config_path)
    if secret_path.exists():
        if secret_path.is_symlink() or secret_path.stat().st_mode & 0o077:
            raise ValueError("统筹本地密钥文件权限必须为 0600，且不能是符号链接")
        saved = json.loads(secret_path.read_text())
        values = config.model_dump(mode="json")
        values["supervisor"] = saved["supervisor"]
        config = AppConfiguration.model_validate(values)
        config._local_connection.update(base_url=saved["base_url"], api_key=saved["api_key"])
    return config


def supervisor_settings_path(config_path: Path) -> Path:
    return config_path.expanduser().with_suffix(config_path.suffix + ".supervisor.json")


def discover_agents() -> list[dict[str, str]]:
    default = offline_default_configuration()
    found = []
    for agent_id, agent in default.agents.items():
        executable = shutil.which(agent.executable) or agent.executable
        if Path(executable).expanduser().is_file():
            found.append(
                {
                    "agent_id": agent_id,
                    "executable": str(Path(executable).expanduser().absolute()),
                    "cli_install_dir": agent.cli_install_dir,
                }
            )
    return found


def build_registration(
    config: AppConfiguration,
    *,
    agent_id: str,
    executable: str = "",
    model: str,
    provider: str = "",
    cli_install_dir: str = "",
    permission_mode: str = "read-only",
    base_url: str = "",
    api_key_env: str = "",
    reasoning_effort: str = "",
) -> AppConfiguration:
    if agent_id not in {"codex", "deepseek"}:
        raise ValueError("Agent must be codex or deepseek")
    detected = next((a for a in discover_agents() if a["agent_id"] == agent_id), {})
    executable = executable or detected.get("executable", "")
    cli_install_dir = cli_install_dir or detected.get("cli_install_dir", "")
    executable = str(Path(shutil.which(executable) or executable).expanduser().resolve())
    if not Path(executable).is_file() or not os.access(executable, os.X_OK):
        raise ValueError("请选择已安装且可执行的 Agent CLI 文件")
    model = model.strip()
    if (not model and (agent_id != "codex" or base_url)) or "REPLACE_WITH" in model or len(model) > 200:
        raise ValueError("请输入实际模型 ID")
    if agent_id == "deepseek":
        if not provider.strip():
            raise ValueError("DeepSeek 必须指定 provider 路由")
        package = Path(cli_install_dir).expanduser().resolve()
        asar = next((p for p in package.parents if p.suffix == ".asar"), None)
        if not (package / "lib/bin.js").is_file() and not (asar and asar.is_file()):
            raise ValueError("请选择包含 lib/bin.js 的 Harness 包目录（支持 Electron ASAR 路径）")
        cli_install_dir = str(package)
    if reasoning_effort not in {"", "none", "minimal", "off", "low", "medium", "high", "xhigh", "max", "ultra"}:
        raise ValueError("无效的思考强度")
    profile_id = f"registered_{agent_id}"
    raw = config.model_dump(mode="json")
    raw["agents"][agent_id] = {
        "user_added": True,
        "adapter": "codex_exec" if agent_id == "codex" else "deepseek_headless",
        "executable": executable,
        "description": "Registered locally; native capabilities require verification.",
        "default_profile": profile_id,
        "cli_install_dir": cli_install_dir if agent_id == "deepseek" else "",
        "enabled": True,
        "allow_execution": False,
    }
    raw["profiles"][profile_id] = {
        "agent_id": agent_id,
        "model": model,
        "provider": provider.strip(),
        "base_url": base_url.strip().rstrip("/"),
        "api_key_env": api_key_env,
        "reasoning_effort": reasoning_effort,
        "permission_mode": permission_mode,
        "credential_env": ["CODEX_HOME"] if agent_id == "codex" else ["DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"],
    }
    updated = AppConfiguration.model_validate(raw)
    updated._local_connection.update(config._local_connection)
    return updated


def register_agent(
    config: AppConfiguration, config_path: Path, values: dict[str, Any], *, replace: bool = False
) -> AppConfiguration:
    agent_id = str(values.get("agent_id", ""))
    existing = config.agents.get(agent_id)
    if existing and not replace and "REPLACE_WITH" not in config.profiles[existing.default_profile].model:
        raise ValueError("此 Agent 已配置；确认替换后重试（CLI 使用 --replace）")
    updated = build_registration(config, **values)
    path = registrations_path(config_path)
    rows = json.loads(path.read_text()) if path.exists() else {}
    # Persist only validated registration fields, never credentials or capability attestations.
    agent = updated.agents[agent_id]
    profile = updated.profiles[agent.default_profile]
    rows[agent_id] = {
        "executable": agent.executable,
        "model": profile.model,
        "provider": profile.provider,
        "cli_install_dir": agent.cli_install_dir,
        "permission_mode": profile.permission_mode,
        "base_url": profile.base_url,
        "api_key_env": profile.api_key_env,
        "reasoning_effort": profile.reasoning_effort,
    }
    _write_registrations(path, rows)
    return updated


def _write_registrations(path: Path, rows: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".agent-registration-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(rows, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def register_supervisor(
    config: AppConfiguration, config_path: Path, values: dict[str, Any],
    *, base_url: str = "", api_key: str = "",
) -> AppConfiguration:
    """Atomically save settings and credentials in a private config-scoped sidecar."""
    raw = config.model_dump(mode="json")
    raw["supervisor"] = values
    updated = AppConfiguration.model_validate(raw)
    updated.supervisor.validate_protocol_options()
    address = base_url.strip() or config._local_connection.get(
        "base_url", os.environ.get(updated.supervisor.base_url_env, ""))
    key = api_key or config._local_connection.get("api_key", os.environ.get(updated.supervisor.api_key_env, ""))
    from agent_center.config import ProfileConfig

    try:
        ProfileConfig(agent_id="codex", model="", base_url=address)
    except ValueError:
        raise ValueError("统筹地址必须是 HTTPS API 地址（本机可用 HTTP），不能包含密钥或查询参数") from None
    _write_registrations(supervisor_settings_path(config_path), {
        "supervisor": updated.supervisor.model_dump(mode="json"), "base_url": address, "api_key": key,
    })
    updated._local_connection.update(base_url=address, api_key=key)
    return updated


def browse_directory(path: str = "", *, files: bool = False, hidden: bool = False) -> dict[str, Any]:
    directory = Path(path).expanduser().resolve(strict=True) if path else Path.home()
    if not directory.is_dir():
        raise ValueError("请选择文件夹")
    entries = []
    with os.scandir(directory) as iterator:
        for entry in iterator:
            if entry.name.startswith(".") and not hidden:
                continue
            is_dir = entry.is_dir(follow_symlinks=True)
            if is_dir or (files and entry.is_file(follow_symlinks=True) and os.access(entry.path, os.X_OK)):
                entries.append({"name": entry.name, "path": entry.path, "directory": is_dir})
            if len(entries) > 2000:
                raise ValueError("目录项目过多，请输入更具体的路径")
    return {
        "path": str(directory),
        "parent": str(directory.parent),
        "entries": sorted(entries, key=lambda e: (not e["directory"], e["name"].casefold())),
    }


def ensure_configuration_editable(store: Any, *, adding: bool = False) -> None:
    runs = store.unresolved_runs()
    if runs:
        run = runs[0]
        raise ValueError(f"任务 {run['task_id']} 的 Run {run['run_id']} 未对账；请打开任务，检查进程与工作区并对账")
    active = store.active_task()
    if active:
        # Adding a new connection cannot rewrite an idle task's existing agent. Replacing
        # connections is also safe before any Run/approval exists (e.g. initial setup failed).
        safe = active["status"] in {"created", "blocked", "paused", "interrupted"}
        safe = safe and active["active_started_at"] is None and not active["pending_approval_id"]
        safe = safe and (adding or not store.runs(active["task_id"]))
        if not safe:
            raise ValueError(
                f"当前任务 {active['task_id']} 保留配置；请打开任务，停止并对账后取消任务，再替换配置"
            )

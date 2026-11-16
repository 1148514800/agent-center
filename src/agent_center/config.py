from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_center.capabilities import AgentCapabilities


class StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AppConfig(StrictConfig):
    data_dir: str = "~/.agent-center"
    default_workspace_root: str = "~/.agent-center/workspaces"


class SupervisorConfig(StrictConfig):
    protocol: Literal["openai_chat_completions"]
    base_url_env: str = "AGENT_CENTER_BASE_URL"
    api_key_env: str = "AGENT_CENTER_API_KEY"
    model: str
    request_timeout_seconds: int = Field(default=120, ge=1, le=900)
    max_output_tokens: int = Field(default=4096, ge=1, le=65536)
    reasoning_effort: str = ""


class LimitsConfig(StrictConfig):
    max_agent_calls: int = Field(default=12, ge=1, le=1000)
    max_supervisor_calls: int = Field(default=40, ge=1, le=10000)
    max_step_retries: int = Field(default=2, ge=0, le=20)
    per_run_timeout_seconds: int = Field(default=900, ge=1, le=86400)
    max_active_seconds: int = Field(default=3600, ge=1, le=604800)
    termination_grace_seconds: int = Field(default=5, ge=1, le=60)
    max_log_bytes_per_run: int = Field(default=52_428_800, ge=1024)
    max_model_input_bytes: int = Field(default=131072, ge=1024)


class PolicyConfig(StrictConfig):
    default_mode: Literal["workspace-write", "read-only"] = "workspace-write"
    approval_default: Literal["deny"] = "deny"
    allow_workspace_write: bool = False
    allow_unverified_capabilities: Literal[False] = False
    allow_full_access: Literal[False] = False
    allow_global_install: Literal[False] = False
    allow_external_publish: Literal[False] = False


class AgentConfig(StrictConfig):
    enabled: bool = True
    adapter: Literal["codex_exec", "deepseek_headless"]
    executable: str
    description: str
    default_profile: str
    cli_install_dir: str = ""
    capability_status: Literal["supported", "unsupported", "unverified"] = "unverified"
    allow_execution: bool = False
    write_sandbox_verified: bool = False  # Legacy metadata; never authorizes execution.
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)


class ProfileConfig(StrictConfig):
    agent_id: Literal["codex", "deepseek"]
    model: str
    provider: str = ""
    auth_mode: Literal["existing_cli"] = "existing_cli"
    permission_mode: Literal["workspace-write", "read-only"] = "workspace-write"
    reasoning_effort: str = ""
    credential_env: list[str] = Field(default_factory=list)


class AppConfiguration(StrictConfig):
    schema_version: Literal[1]
    app: AppConfig
    supervisor: SupervisorConfig
    limits: LimitsConfig = LimitsConfig()
    policy: PolicyConfig = PolicyConfig()
    agents: dict[str, AgentConfig]
    profiles: dict[str, ProfileConfig]

    @model_validator(mode="after")
    def validate_profiles(self) -> AppConfiguration:
        allowed_credentials = {
            "codex": {"CODEX_HOME"},
            "deepseek": {"DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"},
        }
        protected = {self.supervisor.api_key_env, self.supervisor.base_url_env}
        for agent_id, agent in self.agents.items():
            if agent.default_profile not in self.profiles:
                raise ValueError(f"Agent {agent_id} refers to a missing profile")
            profile = self.profiles[agent.default_profile]
            if profile.agent_id != agent_id:
                raise ValueError(f"Profile {agent.default_profile} belongs to a different agent")
            if not set(profile.credential_env).issubset(allowed_credentials[agent_id]):
                raise ValueError(f"Agent profile {agent.default_profile} requests an unsupported credential variable")
            if protected.intersection(profile.credential_env):
                raise ValueError("Supervisor API credentials cannot be forwarded to an Agent")
        return self

    def supervisor_endpoint(self) -> tuple[str, str, str]:
        base_url = os.environ.get(self.supervisor.base_url_env, "").strip()
        api_key = os.environ.get(self.supervisor.api_key_env, "").strip()
        if not base_url or not api_key:
            raise ValueError(
                f"Set {self.supervisor.base_url_env} and {self.supervisor.api_key_env} "
                "in the local environment before making an API call"
            )
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Supervisor base URL must be an HTTP(S) URL without embedded credentials")
        if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Supervisor API requires HTTPS except for loopback testing")
        if parsed.query or parsed.fragment:
            raise ValueError("Supervisor base URL cannot contain a query or fragment")
        if "REPLACE_WITH" in self.supervisor.model:
            raise ValueError("Supervisor model is still a placeholder")
        return base_url.rstrip("/"), api_key, self.supervisor.model


def offline_default_configuration() -> AppConfiguration:
    """Safe static defaults for doctor/help; they cannot execute agents or contact a provider."""
    return AppConfiguration.model_validate(
        {
            "schema_version": 1,
            "app": {},
            "supervisor": {
                "protocol": "openai_chat_completions",
                "model": "REPLACE_WITH_AVAILABLE_MODEL_ID",
            },
            "agents": {
                "codex": {
                    "adapter": "codex_exec",
                    "executable": "codex",
                    "description": "Offline probe only; execution disabled.",
                    "default_profile": "codex_default",
                    "capability_status": "unverified",
                    "allow_execution": False,
                },
                "deepseek": {
                    "adapter": "deepseek_headless",
                    "executable": "/Applications/DeepSeek Harness.app/Contents/MacOS/DeepSeek Harness",
                    "cli_install_dir": (
                        "/Applications/DeepSeek Harness.app/Contents/Resources/app.asar/"
                        "dsh/node_modules/@deepseek-ai/dsh"
                    ),
                    "description": "Offline probe only; execution disabled.",
                    "default_profile": "deepseek_default",
                    "capability_status": "unverified",
                    "allow_execution": False,
                },
            },
            "profiles": {
                "codex_default": {
                    "agent_id": "codex",
                    "model": "REPLACE_WITH_AVAILABLE_CODEX_MODEL_ID",
                    "permission_mode": "read-only",
                    "credential_env": ["CODEX_HOME"],
                },
                "deepseek_default": {
                    "agent_id": "deepseek",
                    "provider": "deepseek-official",
                    "model": "REPLACE_WITH_AVAILABLE_DEEPSEEK_ROUTE_ID",
                    "permission_mode": "read-only",
                    "credential_env": ["DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"],
                },
            },
        }
    )


def default_config_path() -> Path:
    return Path(os.environ.get("AGENT_CENTER_CONFIG", "~/.agent-center/config.toml")).expanduser()


def load_config(path: Path) -> AppConfiguration:
    with path.expanduser().open("rb") as stream:
        raw = tomllib.load(stream)
    config = AppConfiguration.model_validate(raw)
    if "REPLACE_WITH" in config.supervisor.model:
        # Placeholders are allowed for offline setup and reported by doctor; live calls reject them.
        pass
    return config

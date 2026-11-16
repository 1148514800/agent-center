"""Per-agent, installation-bound native capability attestations; never inferred from peers."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from agent_center.config import AgentConfig, ProfileConfig


class CapabilityStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    UNVERIFIED = "UNVERIFIED"


class AgentCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    command_execution_supported: CapabilityStatus = CapabilityStatus.UNVERIFIED
    read_only_supported: CapabilityStatus = CapabilityStatus.UNVERIFIED
    workspace_write_supported: CapabilityStatus = CapabilityStatus.UNVERIFIED
    filesystem_read_boundary_verified: CapabilityStatus = CapabilityStatus.UNVERIFIED
    filesystem_write_boundary_verified: CapabilityStatus = CapabilityStatus.UNVERIFIED
    network_boundary_verified: CapabilityStatus = CapabilityStatus.UNVERIFIED
    approval_response_supported: CapabilityStatus = CapabilityStatus.UNVERIFIED
    structured_command_exit_verified: CapabilityStatus = CapabilityStatus.UNVERIFIED
    cancellation_verified: CapabilityStatus = CapabilityStatus.UNVERIFIED
    installation_identity: str = ""
    evidence_reference: str = ""

    def permits(self, identity: str, required: tuple[str, ...]) -> bool:
        return bool(identity and self.evidence_reference and self.installation_identity == identity) and all(
            getattr(self, name) == CapabilityStatus.SUPPORTED for name in required
        )


def installation_identity(agent: AgentConfig, profile: ProfileConfig) -> str:
    """Invalidate attestations when binary/package or invocation security settings change."""
    executable = Path(shutil.which(agent.executable) or agent.executable).expanduser().resolve()
    paths = [executable]
    if agent.cli_install_dir:
        package = Path(agent.cli_install_dir).expanduser()
        asar = next((p for p in package.parents if p.suffix == ".asar"), None)
        paths.append(asar if asar else package / "lib" / "bin.js")
    digest = hashlib.sha256()
    digest.update(platform.platform().encode())
    digest.update(str(Path.home()).encode())
    if "CODEX_HOME" in profile.credential_env:
        digest.update(os.environ.get("CODEX_HOME", "").encode())
    try:
        for path in paths:
            digest.update(str(path).encode())
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
    except OSError:
        return ""
    digest.update(json.dumps(profile.model_dump(mode="json"), sort_keys=True).encode())
    digest.update(agent.adapter.encode())
    # Parser/launch changes also invalidate old conclusions.
    for module in ("base.py", "codex.py", "deepseek.py"):
        digest.update((Path(__file__).parent / "adapters" / module).read_bytes())
    return digest.hexdigest()


def required_capabilities(mode: str, *, verification: bool = False) -> tuple[str, ...]:
    required = (
        "command_execution_supported",
        "read_only_supported" if mode == "read-only" else "workspace_write_supported",
        "filesystem_write_boundary_verified",
        "filesystem_read_boundary_verified",
        "network_boundary_verified",
        "cancellation_verified",
    )
    return (*required, "structured_command_exit_verified") if verification else required

"""Real CLI failure-path probe with empty credential homes; no product agents are enabled."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from agent_center.adapters.base import AgentRequest
from agent_center.adapters.codex import CodexExecAdapter
from agent_center.adapters.deepseek import DeepSeekHeadlessAdapter
from agent_center.config import AgentConfig, ProfileConfig
from agent_center.runtime.process import ProcessRunner


async def main() -> None:
    output = Path("docs/evidence").resolve()
    metadata = []
    with tempfile.TemporaryDirectory(prefix="agent-center-cli-probe-") as directory:
        root = Path(directory).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        for agent_id, adapter, config in (
            (
                "codex",
                CodexExecAdapter(),
                AgentConfig(adapter="codex_exec", executable="codex", description="probe", default_profile="probe"),
            ),
            (
                "deepseek",
                DeepSeekHeadlessAdapter(),
                AgentConfig(
                    adapter="deepseek_headless",
                    executable="/Applications/DeepSeek Harness.app/Contents/MacOS/DeepSeek Harness",
                    cli_install_dir=(
                        "/Applications/DeepSeek Harness.app/Contents/Resources/app.asar/"
                        "dsh/node_modules/@deepseek-ai/dsh"
                    ),
                    description="probe",
                    default_profile="probe",
                ),
            ),
        ):
            logs = output / f"{agent_id}-cli-2026-11-16"
            logs.mkdir(mode=0o700, exist_ok=True)
            profile = ProfileConfig(
                agent_id=agent_id, model="probe-no-auth", provider="deepseek-official", permission_mode="read-only"
            )
            request = AgentRequest(
                "Return BLOCKED if not authenticated. Do not run any commands.",
                "probe",
                "probe",
                1,
                workspace,
                profile,
                config,
                "No project code or credential reads.",
                15,
                logs,
            )
            spec = adapter.build_launch(request)
            env = dict(spec.environment)
            # Use only generated, empty directories. No original CLI config/auth is touched.
            env["HOME"] = str(root / f"{agent_id}-home")
            Path(env["HOME"]).mkdir()
            if agent_id == "codex":
                env["CODEX_HOME"] = str(root / "codex-state")
                Path(env["CODEX_HOME"]).mkdir()
            else:
                env["DSH_HOME"] = str(root / "dsh-state")
                Path(env["DSH_HOME"]).mkdir()
            env.pop("DEEPSEEK_API_KEY", None)
            env.pop("DEEPSEEK_BASE_URL", None)
            process = await ProcessRunner(termination_grace_seconds=1, max_log_bytes=100_000).run(
                spec.executable,
                list(spec.argv),
                cwd=workspace,
                env=env,
                stdin=spec.stdin,
                timeout=15,
                stdout_path=logs / "stdout.log",
                stderr_path=logs / "stderr.log",
            )
            parsed = adapter.parse(request, process)
            (logs / "events.jsonl").write_bytes((logs / "stdout.log").read_bytes())
            (logs / "diagnostics.txt").write_bytes((logs / "stderr.log").read_bytes())
            metadata.append(
                {
                    "agent": agent_id,
                    "returncode": process.returncode,
                    "timed_out": process.timed_out,
                    "completed": parsed.completed,
                    "error": parsed.error,
                    "event_kinds": [e.kind for e in parsed.events],
                    "commands": len(parsed.commands),
                    "credentials": "empty generated HOME and native credential home; no API keys inherited",
                }
            )
    (output / "cli-failure-probes-2026-11-16.json").write_text(json.dumps(metadata, indent=2))
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

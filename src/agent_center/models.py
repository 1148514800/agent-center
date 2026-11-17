"""Read-only model discovery. No task/thread or project command is created."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import tempfile
from pathlib import Path
from typing import Any

import httpx

from agent_center.config import ProfileConfig
from agent_center.setup import discover_agents


def normalize_codex_models(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    models: dict[str, dict[str, Any]] = {}
    for r in rows:
        if not isinstance(r, dict) or r.get("hidden"):
            continue
        identifier = r.get("model") or r.get("id")
        if not isinstance(identifier, str) or not identifier.strip():
            continue
        identifier = identifier.strip()
        name = r.get("displayName")
        item = {
            "id": identifier,
            "name": name.strip() if isinstance(name, str) and name.strip() else identifier,
            "default": r.get("isDefault", False),
            "efforts": [
                e["reasoningEffort"]
                for e in r.get("supportedReasoningEfforts", [])
                if isinstance(e, dict) and isinstance(e.get("reasoningEffort"), str)
            ],
            "default_effort": r.get("defaultReasoningEffort", ""),
        }
        if identifier in models:
            old = models[identifier]
            old["default"] = old["default"] or item["default"]
            old["efforts"] = list(dict.fromkeys([*old["efforts"], *item["efforts"]]))
            if old["name"] == identifier:
                old["name"] = item["name"]
            if not old["default_effort"]:
                old["default_effort"] = item["default_effort"]
        else:
            models[identifier] = item
    return list(models.values())


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), 2)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


async def codex_models(executable: str) -> dict[str, Any]:
    env = {k: os.environ[k] for k in ("HOME", "PATH", "LANG", "CODEX_HOME") if k in os.environ}
    with tempfile.TemporaryDirectory(prefix="agent-center-models-") as cwd:
        process = await asyncio.create_subprocess_exec(
            executable,
            "app-server",
            "--stdio",
            "-c",
            'approval_policy="never"',
            "-c",
            'sandbox_mode="read-only"',
            "-c",
            'model_provider="openai"',
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            limit=2 * 1024 * 1024,
        )
        assert process.stdin and process.stdout

        async def rpc(identifier: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
            assert process.stdin and process.stdout
            process.stdin.write((json.dumps({"id": identifier, "method": method, "params": params}) + "\n").encode())
            await process.stdin.drain()
            for _ in range(200):
                line = await process.stdout.readline()
                if not line:
                    break
                message = json.loads(line)
                if message.get("id") == identifier:
                    if "error" in message:
                        raise ValueError("Codex 模型检测失败，请检查 CLI 登录状态")
                    result: dict[str, Any] = message["result"]
                    return result
            raise ValueError("Codex 未返回模型列表")

        try:
            async with asyncio.timeout(25):
                await rpc(1, "initialize", {"clientInfo": {"name": "agent-center", "version": "0.1.0"}})
                process.stdin.write(b'{"method":"initialized"}\n')
                rows = []
                cursor = None
                for index in range(10):
                    params: dict[str, Any] = {"limit": 100}
                    if cursor:
                        params["cursor"] = cursor
                    result = await rpc(index + 2, "model/list", params)
                    rows.extend(result.get("data", []))
                    cursor = result.get("nextCursor")
                    if not cursor:
                        break
                return {"models": normalize_codex_models(rows), "source": "Codex 原生模型目录"}
        finally:
            await _stop(process)


async def harness_models(executable: str, install_dir: str) -> dict[str, Any]:
    # Read installed provider metadata through its own resolver, without mounting a task or reading GUI credentials.
    module = Path(install_dir).parent / "dsh-llm-deepseek" / "lib/index.js"
    script = """const m=require(process.argv[1]); const o=m.resolveAdapterOptions({});
const a=new m.DeepSeekAdapter({options:()=>o});
(async()=>{const models=[];for(const x of o.models){const r=await a.resolveModel('deepseek-official',x.id);
models.push({id:x.id,name:x.name||x.id,efforts:r.reasoning?.efforts?.map(e=>e.id)||[],
default_effort:r.reasoning?.defaultEffort||''});}console.log(JSON.stringify(models));})()
.catch(()=>process.exit(1));"""
    env = {k: os.environ[k] for k in ("HOME", "PATH", "LANG") if k in os.environ}
    env["ELECTRON_RUN_AS_NODE"] = "1"
    process = await asyncio.create_subprocess_exec(
        executable,
        "-e",
        script,
        str(module),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 10)
        if process.returncode or len(output) > 1024 * 1024:
            raise ValueError("当前 Harness 未提供可读取的模型目录")
        return {"models": json.loads(output), "source": "本机 Harness 目录（账户可用性未验证）"}
    finally:
        await _stop(process)


async def discover_models(values: dict[str, Any]) -> dict[str, Any]:
    kind = values.get("agent_id", "codex")
    if kind not in {"codex", "deepseek"}:
        raise ValueError("未知 Agent")
    base_url = str(values.get("base_url", "")).strip().rstrip("/")
    key_env = str(values.get("api_key_env", ""))
    # Reuse URL/key-variable validation; never follow redirects with credentials.
    ProfileConfig.model_validate({"agent_id": kind, "model": "", "base_url": base_url, "api_key_env": key_env})
    if base_url and not (
        kind == "deepseek" and base_url in {"https://api.deepseek.com", "https://api.deepseek.com/v1"}
    ):
        key = values.get("api_key") or os.environ.get(key_env, "")
        headers = {"Authorization": "Bearer " + str(key)} if key else {}
        async with (
            httpx.AsyncClient(timeout=12, follow_redirects=False, trust_env=False) as client,
            client.stream("GET", base_url + "/models", headers=headers) as response,
        ):
            if response.status_code != 200:
                raise ValueError(f"模型服务返回 {response.status_code}，请检查地址与密钥")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > 2 * 1024 * 1024:
                    raise ValueError("模型目录过大")
        rows = json.loads(data).get("data", [])
        return {
            "models": [
                {"id": r["id"], "name": r["id"], "efforts": [], "default_effort": ""}
                for r in rows
                if isinstance(r, dict) and isinstance(r.get("id"), str)
            ],
            "source": "API 模型目录（未声明思考强度）",
        }
    detected = next((a for a in discover_agents() if a["agent_id"] == kind), {})
    executable = str(values.get("executable") or detected.get("executable", ""))
    resolved = Path(shutil.which(executable) or executable).expanduser().resolve()
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ValueError("请先选择已安装的 CLI")
    if kind == "codex":
        return await codex_models(str(resolved))
    return await harness_models(
        str(resolved), str(values.get("cli_install_dir") or detected.get("cli_install_dir", ""))
    )

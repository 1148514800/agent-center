# Agent Center

A local macOS-first coordinator for a configured supervisor API and two independently configured CLI agents. The project is being implemented in stages; the real Codex and DeepSeek headless protocols are not declared supported until local compatibility evidence exists.

## Requirements

- Python 3.14 (see `.python-version`) and `uv`.
- A private TOML config copied from `config.example.toml` to `~/.agent-center/config.toml` or selected with `--config`.
- Supervisor URL, API key and model supplied through the environment variables named by that config. Never put secret values into TOML or reports.
- A compatible Codex and DeepSeek Harness headless adapter. `doctor` reports local paths and static compatibility without making paid API calls.

## Install and inspect

```sh
uv sync --locked
uv run --locked agent-center --help
uv run --locked agent-center doctor
uv run --locked agent-center agents
uv run --locked pytest -m 'not live'
```

The project does not change either CLI's configuration. Real CLI/API calls are opt-in and are not run by ordinary tests.

## Commands

```text
agent-center doctor [--live]
agent-center agents
agent-center run "task" [--workspace PATH]
agent-center chat [--workspace PATH]
agent-center history
agent-center show TASK_ID
agent-center logs TASK_ID [--verbose]
agent-center resume TASK_ID
agent-center cancel TASK_ID --yes
```

`--live` is an explicit real-service probe and can incur provider usage. It must not be used unless local credentials, budget, and authorization are in place. At present, do not assume the live path is compatible; consult `docs/COMPATIBILITY.md` and `docs/ACCEPTANCE_REPORT.md`.

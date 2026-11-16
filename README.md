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

## Browser workbench

Start the local Codex-inspired interface:

```sh
uv run --locked agent-center web
```

This opens `http://127.0.0.1:8765` in your browser. Use `--port 8766` to change the port, or `--no-browser` to start without opening a tab. Global options go before `web`, for example `agent-center --config /absolute/path/config.toml web`.

The interface provides task history/search, task creation with workspace selection, persisted follow-up messages, pause/stop/cancel/resume, Plan/Run/budget details, verification evidence, event logs and approval review. Enter sends; Shift+Enter inserts a newline; Cmd/Ctrl+K opens a new task. Task state refreshes every 2.5 seconds. Execution still uses the existing TaskEngine and native capability gates.

Without a config, the UI opens in offline mode and can display history; creating a task records a configuration block. Configure the supervisor endpoint/key/model and verified Agent capabilities before real execution. The UI does not enable disabled Agents. Blocked tasks show their reason; unresolved Runs must be reconciled through the CLI. Approval records cannot grant permissions the native CLI cannot enforce.

The server binds only to loopback and uses same-origin/Host checks plus a per-process CSRF token for mutations. Keep this local service on your own machine; it has no remote-user authentication. No Node build or additional runtime is required.

![Browser workbench](docs/images/web-workbench.jpg)

## Commands

```text
agent-center web [--port 8765] [--no-browser]
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

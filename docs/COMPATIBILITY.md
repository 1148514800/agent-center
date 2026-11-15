# Local compatibility probe — 2026-11-15 (Asia/Shanghai)

This report distinguishes static CLI inspection, deterministic mocks, and real paid/task calls. Static probe commands were run on the current macOS host; no CLI task, API request, credential file read, or sentinel-write attempt was performed.

## Host

| Item | Observed | Result |
|---|---|---|
| OS / architecture | macOS 25.6, arm64 | v0.1 target family; runtime sandbox probes NOT_RUN |
| Python | system 3.9.6; uv-managed 3.12.16 and 3.14.10 | selected runtime 3.14.10 |
| SQLite | system 3.51.0; Python 3.14 sqlite3 3.53.1 | SQLite unit tests use bundled runtime |
| uv | 0.13.6 (arm64 macOS) | lock generated for Python 3.14 |

## Codex

- Executable: `/Users/godblack/.local/bin/codex`; release binary: `/Users/godblack/.codex/packages/standalone/releases/0.166.0-aarch64-apple-darwin/bin/codex`.
- `codex --version` → `codex-cli 0.166.0`, exit 0. `codex exec --help`, exit 0.
- Help confirms JSONL `exec`, stdin prompt, `--sandbox read-only|workspace-write|danger-full-access`, `--cd`, `--model`, `-c`, `--ephemeral`, `--ignore-user-config`, and the dangerous bypass/auto-approve options. The adapter does not emit bypass or auto-approve flags.
- `OPENAI_API_KEY` was unset at the time of the static probe. Stored CLI auth was not inspected or tested.
- JSON event shape, requested model actually used, permission enforcement, sensitive reads, network, extensions, external-write denial, failure/cancel behavior and process tree cleanup remain unverified.
- Static launch mapping supported; actual execution status UNVERIFIED and disabled in the example config.

## DeepSeek Harness

- No `dsh` command on shell PATH. Installed Desktop package is `@deepseek-ai/dsh` `0.2.2-rc.3` inside `/Applications/DeepSeek Harness.app/Contents/Resources/app.asar/dsh/node_modules/@deepseek-ai/dsh`.
- Verified launcher: `/Applications/DeepSeek Harness.app/Contents/MacOS/DeepSeek Harness`; `DSH_NODE_EXECUTABLE` plus `DSH_INSTALL_DIR/lib/bin.js` works with `ELECTRON_RUN_AS_NODE=1`.
- Root CLI help, `--profile headless --help`, and `--dump-default-config` exited 0 without task/API work. Headless flags confirmed: `--json`, `--permission-mode`, `--approval-policy ask|never`, and `--add-dir`. No direct model/reasoning CLI option; root `--patch` accepts per-invocation overlays.
- Shipped headless documentation states that managed GUI credential store/account consumers are disabled; API key routes use launch environment, invocation cwd `.env`, then DSH_HOME `.env`. No environment value, `.env`, profile, or credential was read. The current machine's headless auth is unverified.
- Shipped docs describe a macOS Seatbelt write boundary; this does not verify sensitive-read or network isolation. `never` refuses calls needing approval; there is no live approval-response channel.
- Event JSON, overlay precedence/model confirmation, actual auth, sandbox enforcement and process-group cancellation were not task-tested.
- Static launch mapping supported; actual execution status UNVERIFIED and disabled in the example config.

## Test-layer result

- `U` (unit) and `I` (synthetic process) results are recorded in `docs/ACCEPTANCE_REPORT.md`; they are offline only.
- `L` (real API/Codex/DeepSeek CLI) and `E` (true end-to-end) are NOT_RUN. This session has not sent paid or task-execution requests.
- `doctor` is a static probe by default. `doctor --live` is intended to send one short, tool-free supervisor request; do not run it without an agreed provider, model, credentials and budget.

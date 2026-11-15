# Acceptance report — Agent Center v0.1 implementation in progress

**Date:** 2026-11-15 (Asia/Shanghai)  
**Overall status:** BLOCKED — v0.1 is not accepted.  
**Target runtime:** Python 3.14.10 / macOS 25.6 arm64.  
**Dependency lock:** `uv.lock` for CPython 3.14.  
**Git:** workspace is not a Git repository.

This records observed tests, not intended coverage. `U` means unit/mock, `I` means synthetic subprocess, `L` means real API/CLI, and `E` means real end-to-end.

## Verified offline evidence

| Layer | Command | Result | Evidence scope |
|---|---|---:|---|
| I | `uv run --locked pytest -q tests/integration/test_engine_vertical.py -vv` | Exit 0; 1 passed in 0.68s | Latest synthetic supervisor → fake CLI → workspace pytest → fingerprint-bound evidence → successful state flow. No real CLI/API. |
| U/I | `uv run --locked pytest -q` | Exit 0; 30 passed in 4.17s | Latest offline suite includes mocked API, parser, storage, redaction, policy-gate, fingerprint-freshness and synthetic subprocess/vertical-slice tests. |
| Static | `uv run --locked ruff check src tests` and `uv run --locked mypy src` | Exit 0 | Clean lint and type checks after the 30-test run. |
| Lock/build | `uv lock --check` and `uv build` | Exit 0 | Locked dependency graph unchanged; local sdist and wheel built, not published. |
| U | `uv run --locked pytest -q tests/unit/test_storage.py::test_ambiguous_run_becomes_unknown_not_replayed` | Exit 0; 1 passed | Unknown side-effect Run blocks automatic replay. |
| M0 static | `codex --version`, `codex exec --help` | Exit 0 | Version and help only. |
| M0 static | `ELECTRON_RUN_AS_NODE=1 "$DSH_NODE_EXECUTABLE" "$DSH_INSTALL_DIR/lib/bin.js" --profile headless --help` | Exit 0 | Installed help only. |
| Static doctor | `uv run --locked agent-center doctor` | Exit 5 | Correctly BLOCKED: no central config/model is installed; both adapters remain disabled. No network request. |
| Build | `uv build` | Exit 0 | Local source distribution and wheel produced; no publish/deploy. |

## Real API / CLI / E2E

| Scenario | Status | Evidence / reason |
|---|---|---|
| Real supervisor API answer/tool call | NOT_RUN | No real network request sent; local credentials, model routing, budget and execution authorization were not verified. |
| Real Codex task and sentinel-write denial | NOT_RUN | Help inspected only; authentication and runtime sandbox behavior unverified. |
| Real DeepSeek task and sentinel-write denial | NOT_RUN | Help/docs inspected only; credentials, overlay, runtime events and sandbox behavior unverified. |
| Real supervisor → real CLI → independent verification | NOT_RUN | No paid/task calls made. Both agents are disabled in the example config. |

## Stage status

- **M0:** static metadata and help inspected; real auth, exact model routing, sandbox/permission behavior, sensitive reads, network, hooks, external sentinel, and process cancellation remain UNVERIFIED/NOT_RUN.
- **M1:** package/config, mock-capable API client, typed decision actions, SQLite store, process runner, adapters, history, and a tested synthetic vertical slice exist. Real run path is not validated.
- **M2:** Codex and DeepSeek protocol adapters and centrally snapshotted profile settings exist. Both remain disabled in the sample config. Automatic repair/cross-review and real routing are NOT_RUN.
- **M3:** chat entry, persisted inbox, `/status`, `/plan`, `/logs`, `/ask`, `/add`, `/stop`, `/cancel`, `/pause`, `/resume`, and terminal-task routing are implemented. The synthetic engine slice passes, but interactive input/approval races are not comprehensively tested.
- **M4:** budgets, approvals, output redaction/caps, workspace/test fingerprints, and conservative unknown-run reconciliation are implemented. Reconciliation requires a verified-absent child identity plus explicit operator confirmation; it never kills a stale PID or automatically replays the old plan. Approval records cannot widen backend permissions.
- **M5:** final offline gate passes: 30 tests, Ruff, Mypy, lock check and local wheel/sdist build. Static doctor exits 5 because central config/model values are absent. Real acceptance and clean-install evidence remain NOT_RUN/BLOCKED.

## Security boundaries

- **Enforced by application:** no arbitrary shell for supervisor; strict typed actions; delegate must match current plan; task version CAS on Run creation and finalization; explicit pytest paths constrained under `workspace/tests`; independent evidence is tied to the current workspace fingerprint; unknown Runs block replay; approval is scoped, expiring and default-deny; child env is whitelisted; subprocess argv is passed without shell; pipes are drained and output capped.
- **Behavioral only:** model/agent prompts ask not to leave the task workspace or perform unsafe actions. This is not a sandbox. CLI/native permissions do not prove all sensitive-read/network boundaries.
- **Blocked/unverified:** real CLI sandbox enforcement, auth, provider/model confirmation, cancellation behavior, extension hooks and data-exfiltration limits. Real Agent execution remains disabled by sample config.
- **Approval limitation:** approval is an auditable user decision, not a backend permission grant. Adapters do not widen directories; any capability a backend cannot enforce remains blocked.

## Outstanding blockers

1. Current check commands passed: `uv run --locked ruff check src tests`, `uv run --locked mypy src`, `uv run --locked pytest -q` (30 passed), `uv lock --check`, and `uv build`; static `doctor` exits 5 as expected without local config.
2. Remaining implementation/test gap: interactive chat control races, approval continuation after decision, crash-time process cleanup, database backup/restore and full checklist-wide regression coverage need additional work.
3. Real adapter/API authentication, model routing and permission boundaries need separate explicitly budgeted real tests before either adapter is enabled.
4. No real API/CLI task was invoked; no original CLI config was changed; no permissions were widened; no push, publish or deployment was performed.

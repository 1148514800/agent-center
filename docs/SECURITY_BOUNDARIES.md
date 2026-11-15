# Security boundary status

This local tool is not a sandbox against a malicious CLI, operating system, kernel, or supply chain. Runtime and approvals are fail-closed where a capability is unverified. No original Codex/DSH config or credential file is modified.

## Enforced by this code

- Supervisor has no arbitrary shell tool. Actions are discriminated Pydantic schemas; delegate must match a saved plan step; paths for inspection are canonicalized and constrained to the task workspace; simple sensitive filenames, binary files, oversized text and likely secret-shaped lines are blocked.
- subprocess creation uses an absolute executable and argv array via `asyncio.create_subprocess_exec`; no shell interpolation. The child environment is explicitly constructed and does not contain supervisor API credentials. Stdout/stderr are drained concurrently, log files/data are mode 0600/0700, output is capped, process group receives TERM then KILL on timeout/cancel, and reads are redacted across chunks for credential-labelled environment values.
- Persisted budgets reserve API/agent calls before use. Unknown prepared/running runs become `unknown`/interrupted after restart and prevent another run. Logs/events/SQLite are local sources of task state.
- Approval request is not authorization. A local CLI decision is interactive, requires typing the exact request ID, and is signed by a local 0600 random key. It is bound to task, requirements version, config hash, workspace-contained absolute target, expiry and decision source. It does not create a new scope in an adapter automatically.
- Resume reconciliation is operator-confirmed and displays the saved PID/start/command identity. No stale PID is killed automatically. If the process identity is still live or ambiguous, reconciliation is refused. Confirming reconciliation clears the old plan and never replays the previous Run.
- Schema forbids full access, global installs, publishing, and “approval default allow”.
- Success is a SQLite transaction that checks current requirements, no pending messages, no active/unknown runs, no pending approvals, exact saved criteria, and complete criterion-specific verification evidence. The engine separately fingerprints task workspace contents before/after tests and again before finalization. A file-system edit can still race the database transaction; this is not an OS-level immutable snapshot.

## Restricted by behavior, not hard isolation

- Project contents and model prompts are untrusted; agents are instructed to stay inside the workspace. Workspace path and CLI-native permission preset are included in invocation.
- Codex `workspace-write` does not alone prove sensitive-read/network isolation. DeepSeek headless docs describe a macOS Seatbelt write boundary only; it does not establish network or sensitive-read isolation.
- Provider API costs cannot be hard-capped by this client; usage may be unknown after failures.

## Unverified / blocked

- Both real adapters are disabled in the example config until local, temporary-workspace probes confirm auth, actual model routing, JSON events, success/failure/cancel, external sentinel writes, sensitive reads, network, extension hooks, and child cleanup.
- No security sandbox can guarantee protection from destructive edits inside an approved workspace, malicious task code, unknown secrets or all data exfiltration.
- Approval records cannot enable a capability that the CLI/host cannot enforce. Higher privilege, installation, push, release, deployment and external publication are not in scope.

Do not change `allow_execution`, `capability_status`, policy booleans, CLI profiles or permission modes to make a task run. A blocked task is the intended result while a required boundary is unverified.

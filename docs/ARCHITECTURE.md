# Architecture (v0.1 implementation snapshot)

```text
argparse / prompt_toolkit
        │
        ▼
TaskEngine — one scheduling loop and validated model action dispatch
   ├── ModelClient — OpenAI Chat Completions tool calling (HTTPX)
   ├── CodexExecAdapter ─┐
   ├── DeepSeekHeadless ─┴─ ProcessRunner (argv, two async pipes, group stop)
   ├── workspace read tools / policy checks / evidence gate
   └── ApprovalManager
        │
        ▼
SQLite state + append-only task events + protected run logs
```

`Store` is the task truth source. Changes to task status/messages and their corresponding event are committed atomically. `WorkspaceLock` enforces one scheduler process per data directory. Plans, requirements, approvals, budgets, evidence and CLI snapshots are linked by task/run IDs and requirements version.

Each CLI call is a new process/session; the coordinator carries task context and saved plans. The model may suggest only typed `answer`, `update_plan`, `delegate`, `inspect`, `request_approval`, or `finish_task` actions. No free-form model JSON is executed. The application rejects stale versions, unknown agents/steps, unapproved paths, exhausted budgets, disabled adapters, and incomplete evidence.

The CLI service currently prioritizes safe observable behavior over convenience: `resume` reports context and blocks automatic replay pending reconciliation; real agent execution stays disabled until a local operator has completed and documented explicit safety tests. See `COMPATIBILITY.md` and `ACCEPTANCE_REPORT.md`.

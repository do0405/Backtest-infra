# KRX schedule recovery implementation plan

> For agentic workers: use inline execution with review checkpoints. The user has already authorized implementation and activation.

**Goal:** Resume remote KRX checkpoints automatically when GitHub's native schedule does not fire and verify new local SQL commits.

**Architecture:** A separate local invocation watchdog reads GitHub run state and dispatches the existing single-owner workflow. Collection and SQL import remain in their existing components. Persisted invocation intents plus the unchanged workflow concurrency fence prevent unsafe duplicate launches.

**Tech stack:** Python, official GitHub CLI, Windows Scheduled Tasks, existing PostgreSQL receipt verification.

- [ ] Create behavioral tests in `tests/unit/test_remote_schedule.py` and verify they fail before implementation.
- [ ] Implement the bounded pure admission/reconciliation policy and CLI adapter in `src/backtest_data/ops/remote_schedule.py`.
- [ ] Add `scripts/remote_schedule_watchdog.py` with an OS singleton, durable state, verified existing handoff/configuration, bounded CLI calls, and no overall runtime/retry budget.
- [ ] Add the optional continuation UUID/run name and update the cron minute in `.github/workflows/remote-data-update.yml`; retain existing concurrency and all collection/storage contracts.
- [ ] Review the spec, plan, implementation, and safety tests. Run the targeted tests and static checks.
- [ ] Publish only the workflow, new watchdog module/script/tests, and this design/plan using a targeted atomic GitHub Git tree/commit and non-force fast-forward reference update. Preserve the preceding public workflow and SHA in the operational receipt. Do not push unrelated local edits or secrets.
- [ ] Register a separate AtLogOn watchdog task with IgnoreNew and unlimited execution time. Preserve all existing tasks/processes. Execute one overdue 300-second proof batch through this watchdog.
- [ ] Confirm exact current native/launcher identity, accepted intent/run identity, successful remote KRX capture, and committed Release chain.
- [ ] Wait for the existing local receiver; verify its new SQL receipts and bounded original row/value/clock equality, and confirm replay inserts zero new source rows.
- [ ] Record verification in `artifacts/remote-schedule-recovery-20261011/production-verification.json`, update the existing monitor metadata/prompt, and report factual limits on native schedule and fallback execution.

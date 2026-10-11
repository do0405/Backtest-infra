# DB disconnect recovery and checkpoint resume

**Goal:** Resume stopped KR/US price, financial and publication workers from their saved SQL/audit cursors; a transient DB disconnect must no longer permanently stop them without a bounded recovery attempt.

**Architecture:** CLI owns sanitized classification of database errors. A small shared recovery runner owns bounded subprocess retries, while the existing detached wrappers retain per-domain locks, output receipts and process identity. Dataset availability, SQL clocks, target denominators, provider access policy and migrations stay outside this change. The read-only heartbeat watches wrapper attempt state and current child identity; it does not restart workers.

**Evidence:** All three workers exited `OperationalError` at 2026-10-09 21:51:33 KST. PostgreSQL logged an untracked child exit2, reinitialized active server processes and was ready at 21:51:56. Container identity/start time and restart count were unchanged. Kernel checks found no matching OOM/storage events. The internal child failure's exact cause is unconfirmed; changing PostgreSQL I/O settings or resetting Docker would not be an evidence-backed fix.

**Choice:** Resume once alone leaves the observed permanent-stop behavior; unrestricted retries could hide permanent failures. Use at most six retries with increasing 10/30/60/120/120/120-second waits, only for explicitly classified connection loss/server recovery. Supplier auth/quota/maintenance, semantic SQL errors, invalid receipts, abnormal process exits and normal partial/deferred results are not retry triggers. Each retry uses the same original incremental request or exact audited IDs/cursor, with the domain file lock held throughout. Every attempt has separate receipts and active child identity/time; old receipts remain preserved.

Publication has one fixed 24-hour run deadline across all child attempts. Setup/backoff count toward it; compute remaining seconds before each launch and pass only that remainder to the child. If the deadline expires during an attempt or backoff, do not spawn another child and report `budget_deferred`. Existing batch-boundary soft budgets remain; committed source/evidence/cursor history is never truncated to enforce a deadline. Test late disconnects and deadline expiry during backoff explicitly.

## Tasks

- [x] Write red tests for safe error classification, bounded retries, restart from the same command, no retry on permanent/provider errors and per-attempt receipts.
- [x] Add `src/backtest_data/ops/db_recovery.py` for pure recovery policy and `ops/durable_worker.py` for per-attempt logs, locked local subprocess lifetime and runtime receipts; update CLI diagnostics and make both local detached entrypoints call the shared worker. Keep driver messages/SQL/credentials out of result JSON. Preserve completed price-recovery behavior without adding automatic replay retries.
- [x] Verify the classifier against a deliberately disconnected connection in the separate test DB and run appropriate regression checks.
- [x] Confirm old workers are absent, preserve metadata/results/baselines, launch one price and one financial wrapper plus one audited-publication wrapper from saved checkpoints.
- [x] Verify native process identities, current attempt/runtime, SQL queue/capture or audit progress and E storage; update the existing two-hour read-only monitor for per-attempt identity and retry state.
- [ ] Record current actual counts and remaining source/coverage gaps. Source-blocked original certification remains unqualified; raw row counts do not become clean coverage.

The last full-count snapshot is still the completed 2026-10-09 22:59 KST result; subsequent collection is active and that stale snapshot must not be labeled current. Limited SQL checks confirm attempts, an active KIS overseas lease and new publication verification records. Latest runtime identities and command paths were checked against native descendants after the `20261009T172338Z` resume. The source-level whole-population coverage and semantic traversal remain in progress/pending behind additive concurrent indexes. Further incident and same-volume verification details are in `docs/data/docker-storage-on-e.md` and local `artifacts/clean-completion/db-reaper-recovery/` receipts.

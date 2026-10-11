# Provider quota and cadence implementation plan

> For agentic workers: execute bounded tasks with tests first and independent review.

**Goal:** Keep existing backfill and identities running while enforcing shared
provider budgets and enabling publication-aware refresh after proven completion.

**Architecture:** Add policy/ledger admission at the existing PostgreSQL rate seam.
Use durable preallocation for ephemeral remote KRX; keep operational controls out
of source-data transport. Place completion/freshness decisions at planning seams.

**Tech stack:** Python 3.13, SQLAlchemy, PostgreSQL 16, pytest, GitHub Releases.

## Tasks

- [ ] Capture selected-file baselines, worker identities, current stream/checkpoint,
  database boot and existing migration revision without broad source-table scans.
- [ ] Add failing SQL tests in `tests/integration/test_provider_quota_sql.py` and
  unit transport tests. Implement additive `ingestion/quota.py`, quota models,
  `ingestion/quota_sql.py`, migration `c71302d6b919_provider_quota.py` and Alpaca
  defaults. Preserve `rate_budget_deferred` and legacy caller compatibility.
- [ ] Add failing remote preallocation tests and implement `remote/quota.py` plus
  the narrow hook in `scripts/remote_release_cycle.py`. Carry absolute rate clocks
  through `remote/checkpoint.py` / `remote/worker.py` with bound owned-source state.
- [ ] Add completion/date-boundary tests before implementing phase-aware planning
  in `remote/source_handlers.py` and focused cadence helpers. Keep unfinished
  backfill and refresh retries runnable and never change the local KRX owner fence.
- [ ] Run `.venv/Scripts/python.exe -m pytest` for the changed unit scopes and
  `tests/integration/test_provider_quota_sql.py --require-postgres` against `_test`.
  Review migration/remote admission failures and concurrent-owner cases.
- [ ] Apply only tested additive SQL controls with short lock/statement timeouts;
  record and read back policy values and observed post-activation admissions.
  Publish only allowlisted reviewed remote changes without interrupting a running
  workflow or dispatching another collector. Verify active owner/checkpoint chain.
- [ ] Update operating documentation/quiet monitor and save a sanitized activation
  receipt with tests, policies, transition drain time, worker preservation and
  materially unverified account limits.

Delegation route: native scoped agents for independent SQL admission, cadence
planning and final review. Parent owns remote preallocation, deployment and live
operations. At most two children active; one writer per scope, no recursive work.

# Whole-universe SQL Coverage Implementation Plan

**Goal:** Persist a resumable coverage audit for every member of the original KR/US target manifest, including ETF/ETN and unsupported members, over 2016-01-01 through 2026-10-07.

**Architecture:** Snapshot the target JSONB membership into SQL, then process bounded security batches in transactions. Aggregate observations and decisions in PostgreSQL rather than loading all facts. Store domain summaries and explicit gaps independently of collector terminal/traversal outcomes. Historical calendars, listing eligibility, and fiscal-period expectations remain unknown unless authoritative evidence is supplied; weekday comparisons are always provisional.

**Tech stack:** Python 3.13, SQLAlchemy 2, PostgreSQL 18, Alembic, pytest.

## Scope and integration

Own `src/backtest_data/coverage/`, additive coverage/index migrations following `b830df281926`, and coverage tests. Parent integrates model registration and CLI/operations. Production interaction by this implementation task is restricted to parent-authorized `EXPLAIN` without `ANALYZE`, catalog reads, and index progress inspection; the parent operates production coverage/index workers. No network acquisition is performed.

## Tasks

- [x] Write tests for provisional date gaps, exact date validation, ETF/ETN applicability, and additive migration DDL. Run and observe expected missing-module failure.
- [x] Add pure policy helpers and SQL models/migration. Run focused unit checks.
- [x] Write PostgreSQL tests for full target persistence, missing security retention, collector termination versus clean facts, future exclusion, financial uncertified periods, ETF/ETN fundamentals, chunked resumption, and certificate acceptance/rejection.
- [x] Add start/advance/report/materialize functions. Run on the separate `_test` database only.
- [x] Document module calls and residual uncertainty, then report integration hooks and test evidence to parent.

## Public calls

`start_coverage_run(session, manifest_id, output_start=date(2016,1,1), output_end=date(2026,10,7), watermark=None, expected_market_counts=None)` returns a SQL run ID. Default expected market counts are KR 4507 and US 13251; they are comparison metadata and never alter manifest membership.

`advance_coverage_run(session, run_id, batch_size=25)` materializes one transaction-sized batch. `coverage_report(session, run_id)` returns aggregate summaries and persisted completion state; completion means every target member was audited, never full clean coverage.

`materialize_coverage(engine, manifest_id=None, run_id=None, batch_size=25, max_batches=1, ...)` starts or resumes and commits each batch independently. A positive bounded batch budget is mandatory.

## Semantics

Price clean observed dates require raw/as-traded numeric validity, finite non-null observed volume, and the latest accepted structural decision at the audit watermark. Only `contract_verified` decisions with registered raw rules (`raw_price_structure_v1`, `alpaca_raw_price_structure_v1`, `kis_overseas_raw_price_structure_v1`) count; supplier-zero/unregistered rules cannot silently qualify. The latest decision is joined once using a covering index rather than reading each quote's JSON evidence. Capture time is an audit visibility bound, not historical price acquisition PIT. Future sessions and adjusted observations are excluded. Raw quotes apply no future corporate actions. Provisional weekday gaps include holidays and pre-listing periods; they do not claim missing official trading sessions.

VWAP counts only structurally accepted daily stats with method `matched_daily_statistics` or `trade_aggregated`, evidence `matched_scope_verified=true`, and numerator/denominator scope equal to the stored `trade_scope`. Materialization must precede the audit watermark. Financial observed periods come only from actual source facts. Public-PIT-qualified source periods require the existing exact-document resolver, complete extraction, and accepted source numeric structure. A period is counted as clean observed when at least one certified source fact is present; this does not establish canonical metric inputs or complete concepts for the period. Exact certified corrections are allowed using their own public clock; revision counts are retained. The inclusive end date ends one microsecond before the next local midnight. Financial metrics, accounting semantics, official expected fiscal periods, and warmup completeness are separate unresolved gates. ETF/ETN company fundamentals are not applicable only for explicit security classification; ambiguous products remain unknown.

Collector job status counts and traversal flags are reported separately. Unsupported source jobs remain in the denominator. Missing security links are retained as gaps. Source facts are grouped by document and actual period in SQL; documents are streamed, with no whole-universe fact ORM loading.

Each member also receives explicit corporate-action ledger and historical listing-identity gaps. Existing action events are counted in bounded SQL groups; their presence or absence never certifies complete event/no-event ledger intervals. Explicit ETF/ETN members receive product-fact counts and missing/unqualified product-coverage gaps. No expected product metric set is invented. Index-membership expected universe is `unknown` in global `coverage_run.details.supplemental_readiness`; it does not become an invented per-security missing-membership obligation. Source-unsupported gap counts count job outcomes, while provisional missing-date counts count provisional date positions; aggregate reasons should not be treated as interchangeable unique-security counts.

## Operational use

Register `from .coverage import models as coverage_models` in `db.load_models` and apply migrations through `c71302d6b904` before calling the service. A durable worker can repeat the following after each committed batch and save the returned run ID in its progress artifact:

```python
from backtest_data.coverage.service import materialize_coverage

report = materialize_coverage(engine, manifest_id=original_manifest_id,
    batch_size=25, max_batches=1)
while report["status"] != "audit_complete":
    report = materialize_coverage(engine, run_id=report["run_id"],
        batch_size=25, max_batches=1)
```

Each batch is committed independently. An interrupted or failed batch leaves its SQL cursor unchanged; retry starts at that ordinal. A row lock on the run serializes concurrent advances. Resumption rejects new bounds or manifest arguments and persists missing supplemental metadata on older unprocessed runs. Batch size is 1–100; explicit batch budget is 1–10,000. Current catalog references and collector job state are read at member-audit time, while observation/decision/certificate visibility uses the declared audit watermark. This is an operational audit, not a sealed release snapshot. Collector changes during a long audit can therefore appear in later batches.

`coverage_run` binds the run to the original manifest hash. `coverage_member` stores all target members, including broken security references. `coverage_summary` stores three domain rows per member. `coverage_gap` stores counted gaps with reasons, provisional date spans, and unknown expectation labels. `coverage_financial_period` stores exact observed source period boundaries with source fact/document counts and qualified fact/document counts; its status is `public_pit_qualified_source_period` or `uncertified_public_pit_period`.

Example bounded inspection, using SQL parameters rather than shell interpolation:

```sql
SELECT member.market, member.security_id, member.member->>'symbol' AS symbol,
       period.period_kind, period.period_start, period.period_end, period.instant,
       period.source_fact_count, period.qualified_source_fact_count,
       period.document_count, period.qualified_document_count, period.status
FROM coverage_financial_period AS period
JOIN coverage_member AS member ON member.id = period.member_id
WHERE period.run_id = :run_id
ORDER BY member.ordinal, period.period_end, period.instant
LIMIT 100;
```

The full target defaults to KR 4,507 and US 13,251. These comparison counts never insert or remove target members. `denominator_matches_expected` reports a mismatch explicitly. `audit_complete` means every manifest ordinal was processed, and `coverage_complete` remains false because official historical eligibility and financial expectations have not been certified. Financial observed counts are per security: securities sharing an issuer can share the same source periods and are intentionally separate target denominators.

## Verification evidence

The policy/migration tests first failed for the missing coverage package/migration; PostgreSQL persistence tests first failed for the missing service. Additional regressions first failed for nonfinite/missing volume, unsupported VWAP proxy/scope, certified corrections, next-local-day cutoff leakage, creating an orphan run on invalid batch input, supplemental readiness persistence, supplier-zero/unknown adoption rules, and resuming older run metadata. Final coverage command:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_coverage_policy.py tests/unit/test_coverage_migration.py tests/unit/test_coverage_price_index_migration.py tests/unit/test_coverage_annotation_index_migration.py tests/integration/test_coverage_sql.py -q --require-postgres --basetemp=artifacts/tests/coverage-delivery-final
```

Result: **30 passed** (13 unit and 17 PostgreSQL integration checks). A combined earlier pass verified 69 checks, including 40 independent release-market/scope/database-recovery/worker checks. The integration fixture enforces a database name ending `_test` and rolls back test data. The 17,758-member check snapshots the entire KR/US denominator in SQL, then audits a bounded 100-member batch; no whole-universe production observation scan was used for verification.

## Concurrent index operations and verification

`c71302d6b903` adds `ix_selection_decision_price_coverage_latest` on `(record_id, recorded_at DESC, id DESC)` with included status/rule/evidence-grade columns, restricted to price decisions; it also adds `ix_collection_job_coverage_entity` on job entity ID. `c71302d6b904` adds `ix_validation_issue_financial_completion_lookup` on `(record_domain, record_id, rule, created_at DESC, id DESC)`. These use `CREATE INDEX CONCURRENTLY` inside Alembic AUTOCOMMIT blocks. Index metadata is registered by the coverage model module without editing the original evidence model files.

Production `EXPLAIN` established that the original price query used bounded security and decision-record indexes but did thousands of random heap/JSON reads; the new price projection reduces planned row width from 793 to 140 bytes. Collector lookup performed a whole-job parallel sequential scan for a six-ID batch. Financial pending lookup performed repeated whole-annotation sequential scans for domain/record/rule lookups. These plans justify the three additive indexes; no `ANALYZE` scan was run for diagnosis.

Concurrent builds can take substantial time and consume I/O while collectors keep running. Each migration checks existing invalid indexes before creating anything and checks validity after CREATE before allowing the migration head to advance. An invalid/cancelled build is preserved for read-only inspection and explicit operator repair; neither upgrade automatically drops nor rebuilds it. Migration orchestration must be serialized across AUTOCOMMIT blocks using a session advisory lock, rather than an external `engine.begin()` transaction. The parent integrates this locking into the shared test fixture.

A separate read-only `_test` validity inspection found price and collector indexes valid, but the annotation index had `indisvalid=false`, `indisready=true`, no active build and no index locks despite the old head already being stamped. This exposed a concurrent migration race; post-build guards were added test-first and the 13 unit checks pass. The invalid test annotation index was left intact for the parent's explicit repair. The parent subsequently performed a test-only explicit repair while holding the new session advisory migration lock, with receipt `artifacts/tests/test-annotation-index-repair-20261010.json`; a subsequent independent read-only check confirms all three indexes valid. The empty/tiny `_test` tables choose cheap default sequential scans, so index-only performance on populated production tables is not inferred from that test plan. Functional test success and index readiness are reported separately. Production index builds and final production plan verification are owned by the parent; no production index repair was performed by this task.

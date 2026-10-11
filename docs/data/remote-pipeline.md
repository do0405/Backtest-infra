# Private remote deltas and local SQL import

Status: reversible implementation groundwork. No repository was published, no cloud
account was connected, no provider ownership was transferred, and no existing
local worker was stopped. GitHub execution remains disabled until explicitly
configured. The original target remains **17,758 securities**, with backfill
scope **2016-01-01 through 2026-10-07**; refresh windows are separate.

This implements the transport and SQL contracts in
[the remote design](free-remote-update-local-sql.md) and
[PRD 04](../planning/2026-10-09-local-research-platform/04-remote-ingestion-prd.md).

## Boundaries and format

`backtest_data.remote` owns gzip NDJSON bundles, exact typed values, source FK
closure, a private folder transport, import receipts and independent stream
cursors. Existing provider adapters and `run_collection` perform acquisition.
The remote worker uses `backtest_data.remote_updates.plan_remote_jobs` after
restoring the original catalog, manifest IDs and portable task state in ephemeral
PostgreSQL. Restoration commits before the dynamic planner reads its state.

A bundle contains `rows.ndjson.gz`, optional hash-addressed raw evidence files,
and a final `manifest.json`. Decimal values and dates/clocks have explicit tags.
Datetime values are aware UTC instants. IDs, original capture/publication/
availability/validation clocks and existing eligibility are preserved; a local
import timestamp is recorded separately. No transport operation computes or
promotes PIT eligibility.

The manifest declares schema `c71302d6b910`, codec version, independent stream
sequence, original scope, hashes, compressed/expanded sizes, row counts and a
sanitized task checkpoint. Default limits are 100,000 rows, 512 MiB compressed
per bundle, 2 GiB expanded, 8 MiB per NDJSON line and 128 MiB per manifest. No SQL statements or SQL
dump are accepted. The explicit table allowlist excludes collection jobs, provider
configuration, token caches, rate budgets, leases and release pointers. Audited
derived facts, selection decisions and daily publication proofs are admitted
only with validated lineage closure, including their non-FK input and calendar
references. Missing or unsupported parents block export
or roll back import; stable IDs are never silently remapped.

Source raw files retain their original hashes and bytes. Authentication state is
rejected rather than rewritten. A pagination cursor is admitted only through a
bounded typed `PaginationCursor`; recognized bars/trades/corporate-action source
pages validate the declared pagination field under that same contract. Arbitrary
authentication token JSON remains forbidden.

SQL JSONB numbers are read as text and decoded to exact Decimal values. Import
binds exact JSON text as JSONB, keeping numbers numeric and preserving metadata
such as a provider's `minimum_interval_seconds=0.31` without binary floats.

## Local folder transport and SQL

Example library calls, inside caller-managed PostgreSQL transactions:

```python
from backtest_data.remote.sql import export_delta, import_bundle
from backtest_data.remote.folder import FolderTransport

bundle = export_delta(
    session, {"supplemental_observation": [existing_observation_id]},
    outgoing_directory, stream_id="issuer-products", sequence=1,
    scope=original_scope, raw_root=raw_evidence_directory,
)
head = FolderTransport(private_folder).publish(bundle)
received = FolderTransport(private_folder).pull(
    head["bundle_id"], incoming_directory,
    expected_manifest_hash=head["manifest_hash"],
)
result = import_bundle(session, received, raw_root=local_raw_directory)
```

Export follows actual source-table foreign keys recursively, including source
contracts and raw object metadata. Referenced raw evidence accompanies exported
raw object rows. Import verifies paths, hashes, row schema, values and parents
before recording an immutable receipt in the same SQL savepoint. An outer
transaction must commit before the caller reports success. Any SQL failure rolls
back newly inserted rows and receipt; hash-addressed orphan files are harmless
and may be recovered separately.

Replaying an imported bundle adds zero SQL rows. Importing sequence 2 before
sequence 1 is permitted when all source parents are present; the stream cursor
stays at zero until the missing sequence arrives. Different streams have separate
cursors. A sequence collision or changed existing source row is rejected.

Folder publication orders payloads, manifest, then head. It refuses to exceed its
retained storage cap and never deletes unreceived bundles. Backup acknowledgment
and remote deletion remain unimplemented; no storage here is presented as an
independent full backup.

Large deltas use `export_deltas`, normally 5,000 explicit root IDs per chunk.
Oversize FK closure is split further up to the same per-bundle limits. A contiguous
group reuses parents and raw files emitted by preceding chunks; import order is
therefore stream sequence order. Only its final bundle contains the complete
task checkpoint. `publish_group` preflights the entire retained storage budget,
copies all chunks and atomically advances the final head once. An interrupted
copy or exhausted cap leaves the previous head intact. Restore follows only
head-reachable chunks and ignores uncommitted orphan directories.
`scripts/remote_import.py --private-store <folder> --raw-root <local-raw>` performs
that ordered local import with one transaction per chunk. Its database URL comes
only from the process environment; importing a replayed group adds zero rows.

## Remote acquisition and ownership

The worker admission contract is an external ownership JSON, excluded from data
artifacts. Its default is disabled:

```json
{
  "enabled": false,
  "identity_handoff_ready": false,
  "owner_id": "remote-sec",
  "manifest_id": "b31ee12c-40aa-484a-9807-ed8fafc1ec0a",
  "provider_slots": [
    {
      "source": "sec",
      "account_id": "explicit-account-label",
      "owner": "local",
      "local_enabled": true,
      "market": "us",
      "domain": "fundamentals"
    }
  ]
}
```

Admission requires `enabled=true`, `identity_handoff_ready=true`, `owner=remote` and `local_enabled=false` for
every requested account slot. Those declarations must reflect an actual explicit
ownership transfer; setting JSON does not stop or fence an already running local
process. KIS domestic and overseas slots must refer to the same account label
when they share credentials. A distributed ownership lease spanning local and
remote PostgreSQL is not implemented; deployment is blocked until exclusive
account ownership is demonstrated outside this transport.

`python -m backtest_data.remote.bootstrap --manifest-id <actual-id> --stream-id
<owner-id> --output <outgoing> --private-store <private-folder> --raw-root
<existing-raw> --max-private-bytes <approved-cap>` prepares a private catalog
bootstrap using SQL reads only. It validates all 17,758 original security IDs,
the 4,507/13,251 market counts and original dates, then exports contracts,
securities/issuers, identifier history, current mappings, calendars and directory
raw closure through bounded chunk publication. It excludes old financial and
price numeric history. The selected manifest ID is supplied by the caller.

Catalog bootstrap leaves `identity_handoff_ready=false`. Existing filing,
document and raw natural keys can overlap a later remote acquisition, while the
current producers allocate new IDs in an empty database. A complete identity
handoff/preflight has not been configured. Import rejects those conflicts safely;
it does not remap IDs or change source clocks. Identity compatibility is therefore
an explicit activation prerequisite alongside account ownership.

`scripts/remote_cycle.py` restores private catalog/data bundles, invokes the
actual update planner, submits public job specifications through existing
`plan_job`, and calls `run_collection` with explicit source filters and a bounded
budget. It exports acquired rows and FK closure, then persists the checkpoint in
the same committed bundle. It does not claim that an empty run, recent rolling
window or partial source traversal completed the entire target.

The checkpoint contains every owned job, including filing children discovered
during collection. Source/domain/entity/request/identity, successful receipts,
partial page counts, typed page cursor and original capture anchor survive.
Running jobs become retryable; owner, lease and fencing authority are removed.
Restoration requires explicit `new_remote_ephemeral_database` authority and an
empty operational queue. It cannot overwrite live local jobs. Source watermarks
advance conservatively; unfinished or unsupported jobs remain visible. Every
cycle still plans a current rolling window and retains the original backfill.
An independent stream sequence and previous bundle ID record export progress;
restored original SQL IDs determine the next data delta.

Core source slots receive separate bounded time shares. An extensible handler
stage runs before export for explicitly owned KRX, composite supplemental and
Alpaca supplier-adjusted slots. Use `source=krx, domain=prices, market=kr` for KRX;
`source=supplemental, domain=fundamentals, market=both` requires explicitly owned
SEC, Alpaca and Alpha Vantage dependencies. Supplier-adjusted collection requires
an Alpaca prices slot with `handler=supplier_adjusted` and an explicit supported
adjustment. A plain Alpaca slot continues to request ordinary raw prices.
Supplier state is restored only into an empty fresh operational table; supplemental
file progress and its recent API-call audit history remain portable. Credentials,
token caches and operational SQL state are never included as table rows.

For SEC/OpenDART owners, a reserved bounded SQL-only postprocess step runs
`backfill_daily_publications` against the restored full manifest. It checkpoints
its frozen watermark/document cursor and exports newly committed metadata-grade
proofs without changing strict publication certificates. Additional canonical or
derived materialization may be supplied through the callback; existing unbounded
materializers are not automatically run inside this bounded worker.

KRX uses an explicit rolling fourteen-day and bounded catch-up refresh mode while
retaining the original manifest scope; source-request success remains distinct
from qualified historical coverage. Supplemental and supplier-adjusted handlers
also use separate completed-day refresh windows, retaining independent original
backfill and every unfinished refresh cursor. Their newest and oldest unfinished
windows receive bounded shares. N-PORT planning respects completed report periods.
The supplemental native default keeps its 50 GiB disk guard; the remote adapter
explicitly requires at least 2 GiB free while still enforcing retained storage
and archive bounds. HTTP 404 supplier gaps retain their security identity and
reason in `validation_issue`; authentication/quota failures preserve continuation.
These mechanisms do not establish complete index/ETF/ETN population, complete
VWAP history or completed acquisition of the full target.

## Inactive GitHub workflow

`.github/workflows/remote-data-update.yml` uses standard Ubuntu, ephemeral
PostgreSQL 16.10, Python 3.13.13 and exact workspace dependency versions from
`scripts/remote-requirements.txt`. It restores only a private artifact containing
`bundles/` and `heads/`; it never restores a full PostgreSQL dump or provider
configuration. Acquisition has a 600-second budget. Workflow concurrency prevents
overlapping scheduled runs within this repository.

The workflow only admits a **private repository** with
`REMOTE_PIPELINE_ENABLED=true`, `REMOTE_FREE_BUDGET_APPROVED=true` and
`REMOTE_IDENTITY_HANDOFF_READY=true`. It also
requires an explicit `REMOTE_PRIVATE_STORAGE_CAP_BYTES`, an initial private
whole-target bootstrap artifact named `remote-private-state`, ownership JSON and
the credentials for explicitly owned sources. Configuration is written outside
the uploaded folder. Public repositories are denied because this implementation
has no private R2 transport adapter yet; provider data is never published as a
public Actions artifact.

Workflow credential environment names match the existing loader, including
`KRX_API_KEY` and `ALPHA_VANTAGE_API_KEY`. Credential values stay in process
environment/configuration outside the uploaded private data folder.

The workflow checks retained artifact bytes before collection and again before
publishing. It preserves the previous committed artifact on a failed acquisition
or upload. GitHub artifact retention and other account usage can still limit
availability; private artifacts are a bounded proof mechanism, not indefinite
durable storage or a guarantee of zero charges. No artifact is deleted by this
workflow. Full account usage/free-budget verification, a durable private R2/GCloud
adapter, backup acknowledgments and cleanup policy are deployment prerequisites.

## Verification and missing deployment configuration

Unit tests cover exact Decimal/UTC transport, private-field rejection, path/hash
tampering, allowlisting, folder publish/pull, disabled ownership, portable partial
pagination and dynamically discovered children. PostgreSQL
integration tests cover original clocks/precision, numeric JSONB, replay zero and missing-parent
rollback. Run those only against the existing isolated `_test` database:

```powershell
python -m pytest tests/unit/test_remote_bundle.py -q --basetemp=.pytest_tmp_remote
python -m pytest tests/unit/test_remote_checkpoint.py -q --basetemp=.pytest_tmp_remote_checkpoint
python -m pytest tests/integration/test_remote_sql.py -q --require-postgres
```

Still required before remote activation: selected repository and visibility;
private storage target/account and usable free allowance; bootstrap catalog/raw
closure and fixed manifest IDs; complete source identity handoff/preflight;
explicit provider-account ownership; configured
Secrets; actual scheduled-run and local-pull tests; account usage monitoring and
backup/ack policy. The bounded folder proof restores retained bundles into fresh
PostgreSQL and exports bounded contiguous delta groups; larger histories need
checkpoint compaction and durable private storage. Individual oversized raw
objects exceeding the 512 MiB bundle cap are deferred. No cloud
deployment or monthly cost claim is made by these files.

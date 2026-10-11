# GitHub refresh and local SQL synchronization

Deployment target: `do0405/Backtest-infra`. The user authorized public code,
source data and research results; credentials remain private. KRX is the first
transferred account. Other providers retain their existing local collectors until
their complete identity and account handoffs are verified. The original target
remains 17,758 securities (KR 4,507 / US 13,251), with backfill scope
2016-01-01 through 2026-10-07. Refresh windows are separate.

## Exclusive acquisition and original checkpoints

The continuation manager is fenced by an operator-stop receipt before the
identity-verified KRX wrapper and its native children are stopped. It must not
relaunch that role. A row-lock/COMMIT barrier drains the old server transaction
before a read-only repeatable-read snapshot. Local source jobs, page cursors,
original IDs, source rows, five-minute TTL and fencing tokens are not reset.

The initial handoff contains the 39,330 original KRX jobs and their committed
checkpoints, plus 114,296 original raw metadata identities. Every raw UUID, hash,
path, byte count and original creation clock is preserved. The full target catalog
and its FK/raw closure are exported in bounded chunks. Identity snapshots and
queue restoration are admitted only into a fresh ephemeral database; they cannot
replace a live local queue. Running portable jobs become retryable in the fresh
queue without carrying local lease authority.

A response already captured locally must reuse its original hash identity when
fetched remotely. Conflicting IDs or natural keys fail closed; the importer never
silently remaps them.

## Hosted workflow and durable commit

`.github/workflows/remote-data-update.yml` uses a standard Ubuntu hosted runner,
ephemeral PostgreSQL, exact dependency versions and a two-hour schedule.
Repository concurrency admits one generation at a time. Each bounded batch
reserves export time before runner timeout; the next generation restores the
latest published checkpoint. The first deployment admits only the fenced KRX
slot and exposes only `KRX_API_KEY` to acquisition.

Admission requires `REMOTE_PIPELINE_ENABLED`, `REMOTE_FREE_BUDGET_APPROVED`,
`REMOTE_IDENTITY_HANDOFF_READY`, and a private repository or the user-authorized
`REMOTE_DATA_PUBLIC_ALLOWED` setting. The `REMOTE_OWNERSHIP_JSON` Secret binds
manifest, handoff, account owner and bootstrap hashes. Configuration and keys are
excluded from public data.

The constant bootstrap Release holds `catalog.zip` and `initial-state.json.gz`.
Each data generation publishes a separate Release with `delta.zip`, compact
`state.json.gz` and `commit.json`. Assets upload to a draft; publication is the
sole remote commit point. Incomplete drafts do not advance checkpoints. Tags
bind stream, sequence, run and attempt; published sequence forks are rejected.

Fresh runners restore catalog and latest portable state instead of replaying all
numeric history. Deltas remain available for local SQL replay. The commit binds
asset sizes/hashes, predecessor hash/tag, head ID, sequence and cloud run ID.
Archives reject unsafe paths, symlinks, collisions and oversize content. Expanded
assets are bounded to 512 MiB. This transport does not use expiring Actions
artifacts. It is not a full independent SQL backup or a guarantee of unlimited
GitHub storage.

## Exact SQL import

Bundles contain typed gzip NDJSON and explicit allowlisted tables. Decimal,
dates and aware UTC clocks retain exact values. Source FK/logical lineage parents
and hash-addressed raw evidence accompany rows. Original capture, publication,
availability and validation clocks differ from local import time. Transport does
not promote PIT eligibility.

Export reads parent identities in bounded groups. Import validates existing rows
and inserts dependency waves in batches of at most 100 rows. A changed existing
value, natural-key collision, missing parent or stream collision rolls back the
whole bundle savepoint. Immutable receipts and stream cursors commit with source
rows. Replaying an imported bundle adds zero rows.

## Continuous local synchronization

`scripts/local_sync_daemon.py` polls published Releases, verifies predecessor and
asset hashes, imports contiguous deltas into the existing PostgreSQL and records
progress after commit. A persistent OS lock prevents duplicate daemons; Scheduled
Task IgnoreNew adds a second guard. The importer never acquires provider jobs or
changes their checkpoints.

Temporary network/DB failures retry on subsequent polls. Authentication, integrity,
schema and insufficient-space errors require action. Source permanent failures
travel as sanitized status; quota, maintenance and batch waits remain distinct.
The local 50 GiB disk guard is retained. No source row or raw evidence is deleted.

`scripts/start_local_sync.ps1` is the background entry point. The deployed
Scheduled Task invokes the existing windowless Python executable directly,
without changing PowerShell execution policy. Configuration and
state are under ignored `artifacts/`. The task starts on login; the daemon has no
overall runtime budget. SQL receipts allow safe replay after interrupted file
acknowledgment.

On Windows, `github_config_directory` may bind the already-authorized official
CLI configuration directory across desktop app namespaces. The importer passes
only that directory to the CLI; it does not copy, print or publish credentials.
Fresh download directories use short random names so immutable raw blob paths
remain below the standard Windows path limit. The complete public Release tag
is verified in commit metadata and retained in the import receipt.

## Verification and scope

Isolated PostgreSQL tests cover exact JSONB Decimal/clocks, raw identities, new
multi-chunk inserts, late-chunk rollback, parent closure and zero-insert replay.
Unit checks cover commit lineage, archives and credential rejection. Actual
activation/import receipts live under `artifacts/remote-activation-20261011/` and
`artifacts/remote-sync/`. Tests alone do not prove production acquisition.

The first successful hosted generation on 2026-10-11 was run 38115965869:
10,074 KRX observation rows and 28 response captures were committed to its
published delta, with the original scope and unfinished queue checkpoint. The
earlier run 38115771203 failed before acquisition at a missing runtime dependency;
it remains a separate failed attempt. Run 38116683206 then published another
13,973 observations and 28 captures in sequences 4 through 6. The original
39,330 tasks, subsequent task identities, raw UUIDs/hashes/creation clocks and
published predecessor hashes were verified across both generations. The second
generation advanced 28 tasks from its saved checkpoint. These rows do not
establish whole-period completion.

Manifest hash, structural and typed/private-state checks read one identical byte
snapshot. SQL replay fetches only the existing receipt ID/hash, avoiding transfer
of a potentially large saved queue checkpoint. Source validation, exact-row
comparison and rollback rules remain in force.

KRX quotes are additive evidence. NAV, IV and index values do not establish
complete product history, exact VWAP or historical index constituents. Successful
runs and queue completion differ from whole-period clean completion. SEC/OpenDART,
KIS, Alpaca raw, split prices, VWAP and supplemental collectors remain local in
this first deployment; their credentials are not exposed to the KRX workflow.

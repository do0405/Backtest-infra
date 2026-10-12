# Continuous offline acquisition

The `Offline core refresh` workflow collects US daily price bars and new SEC and OpenDART periodic filings independently of local historical collectors. Its immutable runtime and original target catalog are hash-pinned release assets. Existing KRX collection and local collectors keep their own source and checkpoints.

Each successful run publishes hashed capture bundles, a resumable source checkpoint, and a chained commit manifest as a GitHub Release. Published data releases have no application age expiry. Failed or partial draft releases are not imported. Prices preserve source response bytes; financial captures contain typed numeric extraction evidence and the original response hash. Credentials stay in GitHub Actions secrets.

The target denominator remains the original 17,758 securities. A source watermark means that discovery or traversal was attempted; it does not certify complete market data. Empty price windows retain six-hour retries. Recent fourteen-day price corrections run once per completed weekday. Deferred financial reports remain in the checkpoint for later extraction retries.

Denied SEC archive/index access switches to a resumable scan of all 6,026 target issuers and exact-accession companyfacts evidence. API taxonomy/context is explicitly separate from XML context and remains unqualified. Rejected or oversized OpenDART XBRL keeps its safety bounds and falls back to structured figures only when receipt, corporation, year, report code and financial scope match. Financial retry queues reserve three fresh turns per due retry so old unsupported reports cannot starve current filings.

Source row anomalies and partial financial extraction are retained with validation evidence and incomplete flags; they do not promote unsupported figures or block every later capture. Supplier ticker case-fold collisions are isolated and the remaining exact bulk response is fetched again. Per-run capture metadata is bounded before publication, with a rejected step restored for the next run.

Local login synchronization validates catalog, hashes and the release chain, then resolves native issuer/security identities and commits per-capture SQL receipts. SQL receipts are authoritative after a database restoration; a disk acknowledgment is only a cache. Repeated synchronization does not duplicate captures. The new windowless login task is `BacktestInfra-OfflineCoreSync`, polling every five minutes and retrying temporary connectivity or database outages.

Local SQL import and remote provider health are reported separately. A hash-bound snapshot identifies provider authentication or policy failures requiring action even during old-release catch-up; capacity and network deferrals remain visible as retry status. Large offline backlogs can take time to import while existing collectors use the same local disk.

During replay, progress is written before each release and after every hundred committed captures. It records the current process identity and SQL counters. Provider health remains pending until the bound checkpoint is verified, and any final provider attention state remains visible.

The workflow uses standard public-repository hosted runners with a two-hour job bound, a ninety-minute acquisition budget and export margin. Source budgets reserve separate local and hosted slices before acquisition. OpenDART reservations remain charged conservatively for twenty-six hours, including failed runs. No paid runner or paid storage service is configured.

Service limits still apply. GitHub may delay or drop scheduled runs, public scheduled workflows can be disabled after sixty days without repository activity, and release assets must remain below platform limits. The application splits assets at 512 MiB and updates a useful committed source index after successful capture publication. It has no fixed thirty-day or ninety-day retention limit, but cannot promise unlimited capacity or a scheduler availability guarantee. Re-enabling a disabled workflow resumes from its last committed checkpoint.

Operations require `OFFLINE_CORE_ENABLED` and `OFFLINE_QUOTA_PARTITIONS_READY` repository variables, provider secrets, and private `OFFLINE_CORE_CONFIGURATION` binding catalog/runtime tags and hashes. Runtime upgrades publish a new immutable runtime release and update that configuration only after verification.

Deployment acceptance on 2026-10-12 used `offline-core-runtime-v4-20261012`, SHA-256 `3adc0d6af6c9b021da7d61421418b141abbfbf0e4f6f4bf45305c4e92b484fe7`. Four actual hosted runs committed 7,113 captures. A hash-verified hosted SEC capture stored 283 numeric source facts in local SQL and added zero rows on repeated application. The focused suite passed 168 unit tests and 12 isolated PostgreSQL tests. Existing KRX scheduled execution was observed; the new independent cron is configured and active, with its own schedule event not yet observed at acceptance. Whole-release local catch-up and full data coverage remain separate from deployment acceptance.

Platform contracts: [scheduled workflow behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule), [Release limits](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases), and [Actions limits](https://docs.github.com/en/actions/reference/limits).

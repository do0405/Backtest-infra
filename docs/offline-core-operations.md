# Continuous offline acquisition

The `Offline core refresh` workflow collects US daily price bars and new SEC and OpenDART periodic filings independently of local historical collectors. Its immutable runtime and original target catalog are hash-pinned release assets. Existing KRX collection and local collectors keep their own source and checkpoints.

Each successful run publishes hashed capture bundles, a resumable source checkpoint, and a chained commit manifest as a GitHub Release. Published data releases have no application age expiry. Failed or partial draft releases are not imported. Prices preserve source response bytes; financial captures contain typed numeric extraction evidence and the original response hash. Credentials stay in GitHub Actions secrets.

The target denominator remains the original 17,758 securities. A source watermark means that discovery or traversal was attempted; it does not certify complete market data. Empty price windows retain six-hour retries. Recent fourteen-day price corrections run once per completed weekday. Deferred financial reports remain in the checkpoint for later extraction retries.

Local login synchronization validates catalog, hashes and the release chain, then resolves native issuer/security identities and commits per-capture SQL receipts. SQL receipts are authoritative after a database restoration; a disk acknowledgment is only a cache. Repeated synchronization does not duplicate captures. The new windowless login task is `BacktestInfra-OfflineCoreSync`, polling every five minutes and retrying temporary connectivity or database outages.

The workflow uses standard public-repository hosted runners with a two-hour job bound, a ninety-minute acquisition budget and export margin. Source budgets reserve separate local and hosted slices before acquisition. OpenDART reservations remain charged conservatively for twenty-six hours, including failed runs. No paid runner or paid storage service is configured.

Service limits still apply. GitHub may delay or drop scheduled runs, public scheduled workflows can be disabled after sixty days without repository activity, and release assets must remain below platform limits. The application splits assets at 512 MiB and updates a useful committed source index after successful capture publication. It has no fixed thirty-day or ninety-day retention limit, but cannot promise unlimited capacity or a scheduler availability guarantee. Re-enabling a disabled workflow resumes from its last committed checkpoint.

Operations require `OFFLINE_CORE_ENABLED` and `OFFLINE_QUOTA_PARTITIONS_READY` repository variables, provider secrets, and private `OFFLINE_CORE_CONFIGURATION` binding catalog/runtime tags and hashes. Runtime upgrades publish a new immutable runtime release and update that configuration only after verification.

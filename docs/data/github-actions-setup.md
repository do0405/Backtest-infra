# Public GitHub setup and operating boundaries

Repository: https://github.com/do0405/Backtest-infra (public by the owner's choice).
Source code and chosen data/results may be public. Credentials, authentication
files, local environment, Docker disks, SQL dumps and raw operational logs are
excluded from publication.

Eight named Actions Secrets were registered from existing local provider settings
without putting their values in command arguments or output: KIS_APP_KEY,
KIS_APP_SECRET, OPENDART_API_KEY, APCA_API_KEY_ID, APCA_API_SECRET_KEY, KRX_API_KEY,
ALPHA_VANTAGE_API_KEY and BACKTEST_SEC_USER_AGENT. Future key replacement uses
Settings → Secrets and variables → Actions, or `scripts/github_setup.py`.
There is no need to post keys in chat or edit a public workflow with key values.

`Research and data contract verification` runs the actual framework qualification,
API/isolated PostgreSQL tests and frontend build. A manual trusted run verifies
Secret presence using booleans only, without extra supplier API calls.

`Remote data update` is scheduled but acquisition is disabled while the local
workers own those source accounts. The configured gates are REMOTE_PIPELINE_ENABLED
and REMOTE_IDENTITY_HANDOFF_READY, both false. Before enabling them, a bootstrap
must preserve the original manifest, natural keys/UUIDs, raw hashes, capture clocks,
checkpoints and parents, and the account/source ownership must actually transfer.
Setting a flag alone does not transfer an active local worker's ownership.

REMOTE_DATA_PUBLIC_ALLOWED=true records the owner's permission to use artifacts
in this public repository. The existing transport artifact name
`remote-private-state` is retained for checkpoint compatibility; its name does
not make an artifact in a public repository private. Bounded retention/storage
checks still apply. The initial declared retained storage budget is 128 MiB.

Once a committed data state exists, the local importer can use:

```powershell
python scripts/local_sync.py --repository do0405/Backtest-infra --allow-public-results --private-store artifacts/remote-download/NEW-RUN --incoming artifacts/remote-incoming/NEW-RUN --max-private-bytes 134217728
```

Each download uses a fresh destination and validates hashes/lineage before additive
idempotent SQL import. It never substitutes a test artifact for a data state or
resets the local collector checkpoint. This transport capability does not claim
that all-source cloud collection or automatic local downloads are already active.

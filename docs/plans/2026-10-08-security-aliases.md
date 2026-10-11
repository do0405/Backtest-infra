# Security alias resolution extension

Extends the already approved full US/KR clean dataset identity plan (A3). `005930`, `A005930`, `삼성전자`, and `Samsung Electronics` resolve to one existing security identity. The initially included typo `Samung Electronics` was removed at the user's request on 2026-10-08; append-only withdrawal preserves earlier seals and future seeding cannot recreate it. Prices and financial facts continue to reference existing security/issuer IDs.

## Contract

- Register aliases for every member of the current full target, including ETF/ETN entries. Preserve leading zeros and class punctuation. Normalize Unicode, case, and whitespace under a versioned policy; do not fuzzy-merge securities.
- Reuse append-only `identifier_history`. Widen its identifier field for names. Store alias namespace, normalized key, provenance, assertion key, status, policy version, and recording time in evidence. Accepted and withdrawn assertions append; effective intervals are half-open. Reject naive timestamps and invalid intervals.
- Market/venue/namespace constrain lookup. Multiple matching aliases for one security resolve once; multiple distinct securities produce an explicit ambiguous result. Korean `A` plus six digits is an explicit code format, never a generic ticker rewrite.
- Seed directory codes, ISINs, provider spellings, security names, explicit DART primary stock-code English names, and SEC ticker company names. Preserve ambiguous company names across classes. Samsung curated aliases are anchored to `KR7005930003`; `KR7005931001` and depositary receipts remain distinct securities.
- Current-directory assertions begin at the observed date and become queryable only after recorded availability. They do not qualify historic ticker reuse, listing history, or missing issuer links. No backdating from a current directory.
- A release query resolves exclusively from the release's frozen identifier rows and catalog. Bind the alias policy into the release policy bundle; freeze new rows in a new release without changing prior seals.

## Execution and ownership

Use subagent-driven development for the independent catalog work. One worker owns catalog alias module, model widening, migration, and alias tests. The parent owns release/query/CLI integration, documentation, and live verification. Review after tests. Neither implementation may revert another's edits.

1. Write failing normalization, identity collision, temporal interval, assertion correction, and SQL seed tests; implement catalog registry/resolver and migration.
2. Write failing frozen-release lookup and CLI tests; implement release-bound resolver and commands for whole-target registration and live/frozen lookup.
3. Run real PostgreSQL tests, migrate the local DB, register the entire target, and prove the four confirmed Samsung aliases share one ID while preferred/class variants remain separate. Withdraw the removed typo without changing earlier seals.
4. Seal a new research release, verify frozen queries and historical cutoff rejection, document counts and remaining historical limitations, and commit locally.

## Verification

Required cases include exact/normalized aliases, removed typo non-recreation, leading zeros, common/preferred separation, ticker class punctuation, ambiguous names, market scopes, validity bounds, recording cutoffs, withdrawal/idempotence, all-target SQL seeding, and frozen release isolation. Use the separate PostgreSQL test database; never replace it with SQLite.

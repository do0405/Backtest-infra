# Provider quota and cadence design

The user approved the prior quota/cadence recommendations and requested uninterrupted
activation. Their subsequent steering reduces excessive headroom: Alpaca uses a
0.334-second shared minimum (about 180 requests/minute), KRX permits at most 9,500
managed reservations per Korean calendar day, and OpenDART uses a configurable
19,000-reservation rolling 24-hour guard. DART's actual account allowance/reset is
unverified; this is a conservative project policy, not a provider entitlement.

## Boundaries and invariants

HTTP admission owns shared pacing, reservation accounting and absolute cooldowns.
It does not modify collection jobs, source observations, raw files, ownership,
leases or original checkpoints. Existing workers retain their executable identities.
An additive PostgreSQL policy/ledger and UPDATE trigger on `rate_budget` apply
the pacing/guard to old interpreters without restarting them. Exhaustion arms
`next_available_at`, preserving the existing retryable `rate_budget_deferred`
contract. Generic legacy cooldown updates can be conservatively overcounted;
they must never replenish an allowance. Already committed reservations drain
before full hot-activation enforcement (normally at most 30 seconds).

Remote ephemeral SQL alone cannot protect against a runner crash. KRX therefore
publishes and reads back a separate immutable quota-allocation Release before any
HTTP. Allocations bind the existing owner stream, handoff and bootstrap hash and
hosted run/attempt. Failed/uncertain executions never refund credits. Each possible
Korean date in the workflow lifetime is reserved; SQL admits only allocated credits.
Quota-control tags are outside the `data-` namespace so the old SQL receiver ignores
them. Existing concurrency and checkpoint chain remain authoritative.

Backfill remains continuous. A completed original traversal is established from
the full original denominator and successful source jobs, never an operator stop,
an empty newly restored queue or a rolling freshness watermark. Only then does
dated refresh planning run once per provider-ready date. Pending refresh retries
continue with existing checkpoints. Request completion is distinct from clean
research coverage, which remains false until separately verified.

## Verification

Use failing regression tests before implementation. Test real PostgreSQL admission
with legacy callers, quota edges, failed/retried HTTP, overlapping workers, clock
boundaries, cooldown persistence, portable state validation and restart continuity.
Remote control tests prove publish/readback precedes source HTTP, unconfirmed
publication denies acquisition, repeated attempts cannot reuse consumed credits,
and old data inventory/import ignores quota Releases. Integration tests use only
the separate `_test` database. Live activation uses bounded additive DDL and policy
updates, never a blanket schema upgrade or source-process restart. Deployment
contains only reviewed changes; preserve all unrelated dirty workspace changes.

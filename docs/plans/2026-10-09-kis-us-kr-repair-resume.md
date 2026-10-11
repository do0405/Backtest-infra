# KIS US collection, financial resume, and Korean validation repair

The user explicitly authorizes all three actions on 2026-10-09. Retain the
17,758-entry target (KR4,507/US13,251), 2016-01-01..2026-10-07 output interval,
and every archived response, observation revision and historical decision.

1. Resume pending Korean OpenDART and US SEC financial jobs using existing
   credentials and durable checkpoints. Run a detached, hidden collector with
   process identities and exit receipts; never launch a duplicate.
2. Diagnose Korean validation issues against actual archived KIS responses and
   endpoint requests. Separate request-boundary exclusions, malformed data,
   genuine OHLC/quantity failures and source access failures. Add regression
   tests before changing normalization. Preserve unqualified rows as evidence.
3. Archive official NAS/NYS/AMS KIS masters, match provider symbols exactly to
   the full US target, and plan all US jobs including explicit unmapped gaps.
   Integrate the existing overseas adapter into resumable price collection.
   KR and US KIS reads share one authentication client and account rate bucket.
4. Verify SQL replay, pagination, source blocking, full-target planning and
   immutable provenance. Requeue only diagnosed Korean work with audit evidence;
   do not erase prior rejections or classify unavailable data as complete.
5. Start full KR/US price queues, confirm actual SQL inserts, and update the
   existing two-hour monitor to follow current run metadata and both markets.

The catalog owns present-day symbol/venue assertions. Price storage owns raw
capture validation. The supervisor owns source access, pagination and leases.
Release dependencies include the master archive used by each US price capture.
None of these layers certifies historical identity, corporate-action coverage,
adjusted returns, exact VWAP or full public PIT availability without evidence.

Ownership: one worker handles KR normalization/storage and regression tests;
one handles US catalog/master planning and tests. Parent handles supervisor,
CLI/provider integration, release closure, production queue recovery, live
execution and automation. No recursive delegation or overlapping writers.

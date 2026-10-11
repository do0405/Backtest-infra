# VWAP, supplier price comparison, and index history

Authorized implementation: retain all existing collectors, source clocks, original KR/US 17,758-member universe and 2016-01-01 through 2026-10-07 window. Free official data first. No source values are overwritten or averaged.

## Boundaries and acceptance

1. Trade/statistic acquisition owns matched numerator and eligible volume, session scope, page completeness, source identity, exact decimals and resumable SQL state. A complete Alpaca population using the documented daily high/low AND volume condition rule is exact for that returned scope, not a claim about all SIP trades or historical cancellation/rule coverage. Missing pages, conflicting trades, future sessions or unknown eligibility cannot produce a qualified statistic. Provider bar VWAP times ordinary bar volume is never an exact numerator.
2. SQL VWAP queries own completed session and anchor cutoffs, revision selection and explicit input lineage. Raw cross-session aggregation and split-transformed aggregation are separate policies; an unverified split history cannot certify a constant-share-basis result. Existing strict functions remain compatible.
3. Supplier comparison owns definition/venue/currency/session/reference compatibility and a deterministic source-value decision. Supplier-declared adjusted prices remain usable under the approved policy; comparisons add a grade and never silently equate unspecified KIS adjustments with Alpaca split-only prices.
4. Official index acquisition owns immutable snapshots versus announced changes, publication/effective dates and exact weights. Missing baseline, inaccessible history or absent weights remain explicit gaps. ETF holdings and daily index levels cannot stand in for index membership.
5. Production success requires real official responses committed to SQL and queried back. Isolated PostgreSQL regressions cover paging, future guards, incomplete populations, incompatible adjustments, index publication/effective ordering and immutable replay. A verified small range does not imply full backfill completion.

## Execution

direct_or_delegate: delegate independent price-comparison and index acquisition work; parent owns VWAP acquisition, integration and production verification.
model_tier: inherited
execution_mode: native agents, at most two active, no recursive delegation
rationale: distinct files and data ownership, with integration and live collectors retained under one operator.

Add durable, bounded and resumable jobs only after their SQL behavior is verified; avoid duplicating active original collectors. Register new persisted tables in portable SQL transport where applicable. Remote deployment stays inactive until the user selects a repository/storage and identity handoff is resolved.

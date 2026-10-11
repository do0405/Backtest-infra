# Coverage eligibility evidence and overlays

The eligibility overlay annotates an existing `CoverageRun`. It preserves the
original manifest, all 17,758 members, the 2016-01-01 through 2026-10-07 target,
baseline watermark, cursor, stored summaries, and provisional weekday denominator.
It does not modify raw or clean quotes, financial grades, the financial v8 daily
metadata policy, or supplier declarations. A bounded page remains part of the
original target; its size is not a replacement universe.

## Two evidence axes and profiles

Calendar and security listing history are evaluated independently for each
original provisional weekday. Both axes have explicit unknown and conflict
states. Missing quotes, weekends, failed/empty API responses, and existing
`ExchangeSession` open rows do not establish closed dates.

`qualified` requires authoritative exchange calendar evidence for the calendar
axis and verified, security-specific historical identity and listing intervals
for the listing axis. A calendar can establish an official closed day through an
explicit closed row or a declared complete nonempty interval. An open day with
unresolved listing history remains unknown.

`provider_declared_research` also admits complete broker calendar responses and
explicitly bound source-declared listing dates. Its reported counts remain
separate from verified counts. Alpaca calendar results retain provider grade;
KRX listing date plus source ISIN retains source-declared identity grade.
Neither changes the status of the historical target universe.

Listing intervals use inclusive starts and exclusive ends. Evidence has a
separate inclusive scope interval: a source snapshot does not extend beyond its
requested/report date. Verified lifecycle intervals can be grouped by an
explicit lifecycle reference, including relisting intervals. Union is permitted
only within one immutable declaration set identified by the exact tuple of
`lifecycle_ref`, source, capture clock, and
`source_refs.lifecycle_declaration_ref`. Distinct captures/declarations stay
independent even when they reuse the same lifecycle reference. Exclusion outside
verified intervals requires a complete lifecycle declaration. A provider's
initial listing and effective delisting dates can provide reported exclusions
within the declared source scope. Ticker/name/current-alias matches do not
provide verified historical identity. A Form 25 filing date is not a delisting
effective date. Contradictory `snapshot_scope_valid=false` observations stay
rejected.

Conflicting accepted assertions remain conflicts. Any baseline-visible quote on
a declared closed or unlisted date also becomes a conflict, rather than an
exclusion. All dates remain in the original provisional denominator.

## Imports

`coverage.eligibility_service.import_calendar_evidence` accepts a SQLAlchemy
session and keyword arguments:

- `market`, `source`, `calendar_version`;
- `returned_rows`, `start`, `end`;
- `captured_at`, immutable `source_refs`, and optional actual `recorded_at`;
- explicit `complete_interval` and `authoritative` declarations.

Rows are normalized objects with `date` (or `session`) and `status` (`open` or
`closed`). Calendar API rows without status are explicit returned open sessions;
their other fields remain intact. Dates must be unique and within the declared
range; a complete empty response is rejected. Imports accept at most 10,000 rows
and a twenty-year interval. Source references must identify original raw bytes,
the immutable source document, or capture. The importer makes no provider calls.

An authoritative assertion additionally requires
`source_refs.authority='official_exchange'` and
`source_refs.calendar_scope_verified=true`. These are explicit proof assertions
from a trusted evidence acquisition/review path, not a source-brand shortcut.

`import_listing_evidence` accepts `security_id`, `market`, `source`,
`listing_start`, optional exclusive `listing_end`, `scope_start`, `scope_end`,
capture/record clocks, source references, evidence, `identity_status`, `grade`,
`complete_history`, `initial_listing_declared`, and optional `lifecycle_ref`.
Complete history also requires the original immutable
`source_refs.lifecycle_declaration_ref` identifying the whole interval set.
Bound source-declared evidence requires
`evidence.security_binding.security_id` to match the selected security. Verified
evidence additionally requires `historical_identity_verified=true`,
`listing_interval_verified=true`, and binding basis
`historical_security_identity`. Unsupported proof or contradictory dates are
persisted as rejected assertions, preserving their rejection reasons.

`import_krx_listing_evidence` reads one existing `KRXQuoteObservation` by ID with
an explicitly supplied `security_id` and matching `source_isin`. It preserves
observation/raw/capture/contract references, requested date, listing date, ISIN,
and issues. It creates source-declared evidence only.

`import_supplemental_listing_evidence` reads one existing listing observation by
ID, retaining its document/raw/hash references, IPO/delisting/report dates,
fields and issues. Its identity defaults to `unresolved_symbol`. Explicit
`source_declared_binding=true` enables a reported current-symbol declaration,
never verified history. Invalid snapshot scopes are rejected in either case.

Evidence is append-only at the database level. Assertion hashes exclude the
local recording clock, making an exact capture replay idempotent. A replay
retains the original recording clock. Capture and recording clocks must both
fall within the overlay cutoff; importing an old capture now does not backdate
its visibility. Acquisition callers should use the actual default recording
clock, reserving explicit clocks for faithful archive replay and isolated tests.

## Materialization and reads

`materialize_eligibility_overlay(session, run_id, limit=25, cursor=None,
evidence_cutoff=None, profile='provider_declared_research')` creates one immutable
page of at most 100 members. The first page requires an explicit aware cutoff
at or after the original baseline watermark. Subsequent pages use the returned
opaque `next_cursor`, the same baseline run/profile/cutoff, and a bounded limit.
An already materialized page replays exactly and requires its original limit.
The next baseline page must already have been audited; unavailable baseline
results do not advance the overlay.

The overlay records the original manifest hash, watermark, target count and
dates together with its own evidence cutoff, policy and profile. Each page
records a deterministic evidence hash and a content hash linked to the previous
page. Evidence reads are limited to 1,000 assertions per selected page scope;
overflow fails explicitly and rolls back, allowing a smaller member page.
Calendar blobs may contain the declared bounded interval. Quote reads select
only dates for selected securities within the original capture/date bounds.

Missing baseline weekday spans reconstruct the original clean weekday positions.
Original stored quote counts remain labeled separately when quotes fall outside
that grid. Every position appears in one disjoint span and exactly one category:
verified/reported closed, verified/reported not listed, verified/reported
eligible clean or missing, unknown, or conflict. Independent calendar/listing
counts also each conserve the grid. Weekend observations do not add a new
provisional position.

The caller owns the outer commit. Imports and page materialization use savepoints;
an exception rolls back the page and cursor together. Database immutability
triggers reject updates/deletes of assertions, overlay metadata and pages.

`eligibility_overlay_report(session, overlay_id, limit=25, cursor=None)` reads
stored results with at most 100 returned members and at most 100 source pages.
It reports the materialized ordinal, completion flag and latest content hash.
Completion means all baseline members have annotations; it does not certify
coverage, exchange calendars, or historical universe membership.

The migration is `c71302d6b918`, following `c71302d6b917`. Unit tests cover unknown
versus closure, boundaries, relisting/ticker reuse, conflicts, late evidence and
span conservation. Isolated PostgreSQL tests cover immutable/idempotent imports,
bounded replay/reporting, rollback, adapters and original baseline preservation.

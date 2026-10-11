# Completed-source refresh policy

These are project scheduling choices. They are not provider publication guarantees.
KRX's official publication hour is unverified. The financial-commission DataGo
stock-issuance service's next-business-day 13:00 description is not a KRX rule.

| Source | Earliest new dated refresh | Eligible date |
| --- | --- | --- |
| KRX | 09:00 Asia/Seoul | Previous Korean calendar date; weekends remain explicit requests |
| KIS Korean daily prices | 09:00 Asia/Seoul | Previous weekday |
| OpenDART filing discovery | 09:00 Asia/Seoul | Previous calendar date |
| Alpaca and KIS overseas daily prices | 06:00 America/New_York | Previous weekday |
| SEC filing discovery | 06:00 America/New_York | Previous calendar date |

Before the gate, the preceding eligible date remains current. New York's clock
uses its actual daylight-saving offset. Weekday filtering is not a qualified
exchange holiday calendar. All receipts retain `coverage_complete=False`.

## KRX phase transition

Unfinished original backfill retains its existing bounded planning, catch-up,
rolling refresh and collection throughput. The completed-phase gate requires the
original planning cursor through 2026-10-07 and all 39,330 unique original
service/date keys across 2016-01-01 through 2026-10-07. Each original job must be
scoped to the frozen manifest, lack `refresh_anchor`, and have succeeded. Empty
queues, rolling watermarks, operator stops and refresh jobs do not establish this
proof. New completed-phase plans use one stable anchor per eligible provider
date. The collector still resumes pending jobs before the next date is due.

## Local manager activation and continuation

The optional `--refresh-manifest-id` argument on
`artifacts/continuous/manager.py` enables the completed-pass hook for `prices`,
`alpaca_prices` and `fundamentals`. The exact original manifest must be supplied;
the hook never guesses a target from the latest SQL row. It does not enable local
KRX. Its `operator_stopped` state remains an ownership fence.

Activate at the manager boundary: preserve the full existing manager runtime in
the new manager run directory, including every generation and uncertain launch
journal; run the manager with the same singleton lock and the explicit original
manifest. Existing source wrappers and children are adopted without stopping
them. A manager already in memory cannot import this hook merely because files
changed. No source checkpoint should be reset or replaced for activation.

After the old wrapper has finished, planning obtains the original role mutex,
refreshes process inventory under that mutex, and rechecks runtime metadata and
operator-stop fences. Missing, unknown or failed original work prevents the
completed-pass transition. Local completion requires the full original security
root denominator, and for fundamentals every mapped issuer-discovery root.
Terminal `unsupported` work is reported as an explicit traversal gap. It never
establishes successful historical coverage.

New work comes from `build_update_jobs` with provider-aware cutoffs. The latest
whole completed refresh root denominator advances each source's catch-up date,
including explicit unsupported gaps. Catch-up spans at most 31 calendar days;
prices retain their 14-day correction window and filing discovery its 90-day
window. Planning commits at most 250 roots per tick before publishing its frozen,
hash-bound cursor. Native launch waits for the committed full plan and follows
the existing launch journal and process/lock admission. Existing pending refresh
work is resumed independently of the publication gate.

Filing work is bound to issuer/accession and relevant document metadata rather
than the moving discovery window. An unchanged accession reuses its existing
job and extraction evidence, including legacy jobs. New accessions and changed
document identities retain new work and manifest lineage. Research qualification,
historical public availability and clean dataset releases remain separate work.

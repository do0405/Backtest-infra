# Matched VWAP and supplier validation

The original scope remains KR 4,507 + US 13,251 = 17,758, 2016-01-01 through 2026-10-07. Existing collectors are preserved. A completed HTTP traversal is separate from an eligible clean dataset.

## US matched inputs

`prices/vwap_inputs.py` pins Alpaca's documented daily **high/low AND volume** condition table by tape. It aggregates all returned trades in the New York calendar day, including any eligible opening/closing print. Unknown conditions and malformed eligible records block qualification. This is exact for that returned population and declared rule, not certification of historical rule versions or a complete cancellation stream. General bar volume and provider bar VWAP are never multiplied into an exact numerator.

`vwap_trade_run` and `vwap_trade_event` checkpoint each page in SQL. Source documents and hashes remain immutable; token chains, request parameters, conflicts, complete pagination and Decimal arithmetic are checked. The tape/venue/ID/nanosecond key permits source IDs reused at different times and deduplicates an overlapping identical event. Different RFC3339 spellings normalize identically. Earlier failed runs remain in SQL under their original policy key.

Only a terminal, issue-free page chain creates `vwap_daily_stat`. Strict identity qualification requires a dated, explicitly verified ticker binding. Today's literal ticker without historic binding retains exact source N/D but is provisional. It cannot silently certify ticker reuse or a historical investable universe.

The full-universe worker starts at the original end day and traverses all original US members, then earlier weekdays; empty holiday responses remain source results, not an exchange-calendar or suspension certificate. Runs are bounded by time/pages and a 50 GiB disk guard. Continuation lives in SQL and the manifest/hash/policy-bound progress file. The initial detached worker has a 24-hour/1,000-page budget; budget expiry preserves scope and is not completion.

The current input policy permits zero-size/zero-price records only when their known conditions exclude them from both sums; eligible trades remain strictly positive. Same-member official Nasdaq CQS aliases are used for trade API symbols (for example, ACT `BIP$B` maps to CQS `BIPpB`). This does not establish historical ticker identity. An initial 400/404 creates a bounded security/day gap; a failed continuation, authentication failure or quota failure blocks the traversal. A null trade array does not certify no trading. The original failed worker exited on the ACT symbol before this mapping was connected; only that VWAP worker was resumed at its saved cursor after termination and released locks were confirmed.

Archived failed pages can be revalidated without API calls under a new policy-bound run. The 2026-10-10 08:38 UTC verification batch processed 13 runs into valid numeric populations, preserving original captures and failed-run history. This does not establish their historical identities or complete coverage. Current process and cursor paths are always read from `artifacts/kr-full-workers.json`; transient Windows file-sharing errors in a status read are bounded retries, not a collector/SQL failure diagnosis.

```powershell
.venv\Scripts\python.exe -m backtest_data vwap-inputs --manifest-id b31ee12c-40aa-484a-9807-ed8fafc1ec0a --progress-path artifacts/vwap-inputs/progress.json --budget-seconds 600 --max-pages 20
```

Do not run this command alongside the active detached worker. File and SQL advisory locks prevent duplicate writers. Existing Alpaca raw/split and supplemental reference workers remain separate and share the provider call budget.

## Queries and anchors

`prices.vwap_sql.query_vwap_sql` accepts an ordered explicit session grid, aware cutoff, scope and optional anchor knowledge time. It blocks future/uncompleted days against both the cutoff and actual current time, missing sessions, incompatible contracts and future anchors. Default queries require dated ticker identity. `allow_current_catalog=True` is an explicit research path with `historical_identity_verified=false`; it is not a complete clean release. Optional statistic IDs pin immutable calculation inputs.

Native trade-unit period/Anchored VWAP is `sum(N)/sum(D)`. A split-adjusted constant-share-basis calculation still needs verified split evidence; the original strict functions in `prices/vwap.py` and `prices/adjustments.py` retain that behavior. Multi-day raw-unit averages crossing a split are not presented as split-adjusted VWAP.

Actual production ACNT 2026-10-06/07 inputs yielded N=258936.5250, D=18781 and research Anchored VWAP=13.787153239976572067515041797561365209520259837069. The default strict identity query remains unavailable. `artifacts/vwap-inputs/production-verification.json` records the timestamp, exact input IDs, page documents and future-day rejection; two verified numeric days do not mean US backfill completion.

## Korea

`store_krx_pair` connects same-response ACC_TRDVAL/ACC_TRDVOL to a same-date ISIN basic row and a supplied explicit unit contract. It preserves the provider's reported daily scope rather than claiming regular-only, NXT or consolidated market coverage. Missing units, conflicting dates or unresolved historical short-code identity block promotion. Production 2026-10-07 Samsung quote has a reported date, but its corresponding basic row has `reported_date=NULL`; the strict canonical connection therefore remains unavailable. The original KRX typed amount/volume inputs remain preserved in SQL.

## Supplier prices and indices

See [supplier-price-comparison.md](supplier-price-comparison.md) and [index-history.md](index-history.md). Two-of-three agreement never averages prices or equates unknown adjustment definitions. Actual KIS raw and Alpaca split are not equivalent inputs. Official change lists cannot replace a missing historical index baseline or weights; unknown PDF public dates remain null.

New immutable index records and all VWAP page/identity dependencies are in the private SQL transport allowlist. Operational trade checkpoints require persistent SQL; the ephemeral remote source scheduler does not yet own this new VWAP handler. Remote deployment remains inactive until repository/private storage and identity handoff are configured. No existing sealed release was rewritten or marked complete by these changes.

## Final verification for this implementation

The final targeted run passed 68 unit tests and 33 isolated PostgreSQL tests; one optional live-download test was skipped without `--run-live`. Actual official responses and production SQL readback were separately verified as described above and in the index document. Compile checks and normal Git whitespace checks passed. At 2026-10-10 08:46 UTC, SQL held 58 daily statistics and the active traversal had committed 91 pages through member cursor 61 on 2026-10-07. These are a timestamped progress snapshot, not full coverage. `artifacts/vwap-inputs/thread-verification.json` links the evidence. The existing two-hour monitor follows the latest worker metadata and remains active.

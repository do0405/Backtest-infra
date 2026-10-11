# Free official supplemental sources

The original target remains KR 4,507 + US 13,251 = 17,758 securities, including ETF/ETN, for 2016-01-01 through 2026-10-07. Existing collectors, security identities, price/fundamental rows, publication evidence and releases are preserved.

## Storage and execution

`supplemental_document` preserves source contracts, response hashes, original capture times and public request parameters. `supplemental_observation` stores typed dates and exact numeric quantities, amounts, weights, assets, fees and trade numerators/denominators, alongside original fields in JSONB. Both are immutable. `canonical_eligible=false` is enforced by SQL: successful access alone never adopts a security identity or proves historical eligibility.

Report/reference, filing, public, announcement, effective, ex, record, pay, IPO and delisting dates are separate. Unknown public dates remain null. Current catalogs or holdings obtained after the output cutoff are diagnostic reference snapshots, not historical backtest inputs. A requested snapshot date does not override a contradictory source IPO/delisting date.

The separate `auxiliary.supplemental_sources` worker uses an independent Windows file lock and detached WMI wrapper. It checkpoints successful commits, retains failure history, shares existing SEC/Alpaca call budgets and has a seven-day execution budget and a 50 GiB free-space guard on E. Budget expiry preserves scope and cursor; it is not completion. Source failures defer that source without restarting existing collectors. N-PORT keeps a live streaming iterator between batches and scans to the saved TSV cursor once after restart.

## Connected sources and remaining gaps

| Source | Connected SQL inputs | Remaining limitations |
| --- | --- | --- |
| Alpha Vantage LISTING_STATUS | Whole active/delisted lists at explicit historical dates; snapshot, IPO and delisting dates | Free 25 attempted calls per rolling 24 hours within this project; external use of the same key is not controlled. Source discrepancies, ETN completeness and ticker reuse remain unverified. |
| SEC quarterly EDGAR indexes | Form 25/25-NSE and amendments, CIK, accession path, filing date | A delisting filing is not the last trading day or a complete historical listing master. |
| SEC fund ticker mapping | Current CIK/series/class/symbol reference | Not a historical ticker or ETF classification proof. |
| SEC N-PORT quarterly files | Accession/series, report and filing dates, assets/liabilities, holding identifiers, amounts, quantities and weights | Filing is not dissemination. Funds include mutual funds; ETNs are outside this form. Only 2019Q4 download verified initially; other planned quarters need individual retrieval. 2016 through earlier 2019 is a source gap. |
| KODEX/TIGER official downloads | Whole current catalogs, dated PCFs and distributions | Historical closed products/all issuers are absent from current catalogs. TIGER's requested PCF date lacks independent response confirmation. KODEX live current-price fields inside historical PCFs are excluded from historical valuation. |
| iShares/BlackRock | Whole ETF catalog, workbook holdings and distributions | A current workbook is not a complete historical holdings archive. Requested historical CSV dates may be ignored; response dates are authoritative. Other managers remain unconnected. |
| Shinhan official ETN | Product codes/fees/index names and issuance quantities/amounts | Some issuance rows lack identifiers. Maturity/public dates and all other issuers remain incomplete. |
| LSEG/FTSE Russell | Paginated official announcement catalog, declared announcement/effective dates | Full notices may require a portal subscription; complete starting constituents and weights are unavailable. Catalog includes global families, not only US indexes. |
| S&P official releases | Reachable constituent addition/deletion tables and separate announcement/effective days | Initially one 2016 release; complete release archive and historical weights are not connected. |
| KRX index composition website | Access investigation only | Website denies access; no bypass. Existing approved daily index-value APIs continue separately and do not provide constituent history. |
| Alpaca corporate actions | Whole-market, all-type yearly pagination and exact action terms/ex/record/pay dates | Search process date is not announcement/economic date. No independently validated action ledger or two-independent-source agreement yet. |
| Alpaca SIP trades | Resumable whole-session reference traversal, exact candidate matched amount/volume inputs | Literal regular-sale-only condition subset is narrower than full-market VWAP. Full US trade backfill requires a validated condition/session/identity policy and action coverage. This reference traversal does not shrink the US target. |

Alpaca bridges into existing corporate-action and VWAP functions require explicit independent proofs. Incomplete pages, ambiguous security identity, unknown trade conditions, uncovered action intervals and future events do not become verified inputs. Candidate trade aggregates are never constructed as provider bar VWAP multiplied by general volume.

The Alpha Vantage traversal declares all 3,933 calendar dates and both states (7,866 requests), prioritizing end/start reference snapshots. At 25 requests/day, an exhaustive daily traversal needs at least 315 daily budgets before failures or other key use. Free alternatives connected here are official SEC delisting filings; Nasdaq free current directories are useful current references, not complete historical replacements. Historical Daily List, full index notices/weights and some closed-product data may require paid access; no purchase was made.

## Verification

Read actual commits with `.venv\Scripts\python.exe artifacts\supplemental\sql-status.py`. It checks a bounded set of documents using their indexes, with exact row counts, typed date/numeric samples and rolling Alpha Vantage attempts. This is not a full aggregate count. Large supplemental aggregation timed out under concurrent I/O; the bounded verification succeeded. It does not rescan the original large price tables or print credentials. Operational metadata and progress are in `artifacts/kr-full-workers.json` and `artifacts/supplemental/progress.json`.

First verification at 2026-10-10 14:20 KST confirmed 4,502 observations: 1,000 Alpaca corporate actions, one trade aggregate, 1,000 N-PORT filing rows, 898 issuer holding rows, 108 distributions, 395 delisting filings, 50 LSEG announcements, ten S&P member changes and 1,040 product/issuance rows. These are early commits, not full historical or clean coverage. Follow the later timestamped SQL verification artifact for new counts.

Verification at 14:31 KST confirmed Alpha Vantage active/delisted snapshots: 2026-10-07 had 14,509/9,548 rows and 2016-01-04 had 8,167/947 rows, totaling 33,171 observations. N-PORT fund and holding numeric rows were separately confirmed, with public dates null. `nport-numeric-verification.json` records exact SQL examples. KODEX's 2016-01-01 response date did not match the requested date; it was archived as an explicit gap rather than backdated.

Trade aggregate rows are cumulative page checkpoints. Never sum successive checkpoints as independent daily amounts. Only a complete final candidate with the same declared population and separately verified bridge proofs may feed VWAP functions.

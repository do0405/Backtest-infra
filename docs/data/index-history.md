# Official index history source staging

The original target manifest remains 17,758 securities (KR 4,507 / US 13,251), with output dates 2016-01-01 through 2026-10-07. These are the acquisition denominator and cutoff, not a claim that the public index sources cover every target or every historical date. This implementation has acquired real public **change lists**; it has not obtained a complete historical membership baseline or constituent weights. ETF holdings, fund portfolios, index levels, market capitalization summaries, and indicative future portfolios are not index membership evidence.

## Interfaces

Import `backtest_data.indices.models` during model loading. Migration `c71302d6b913` follows `c71302d6b912` and contains frozen DDL, without importing runtime models. Parent integration owns CLI/model loading and remote registry wiring.

```python
from backtest_data.indices.acquisition import (
    collect_index_history, project_supplemental_history, PublicIndexClient,
)
from backtest_data.indices.requests import public_download_requests, default_requests

# An explicit independent job; existing source collectors are never invoked.
collect_index_history(
    engine, raw_root, manifest_id=original_full_manifest_id,
    requests=public_download_requests(), job_key="russell_public_downloads_v1",
    max_requests=2, budget_seconds=60,
)

# Reuse existing official LSEG/S&P immutable responses without another fetch.
project_supplemental_history(
    engine, manifest_id=original_full_manifest_id, limit=100, budget_seconds=30,
)
```

Repeat with the identical plan and job key to resume. Changing the plan requires a new explicit job key. `default_requests()` prioritizes ten discovered public Russell PDFs and then queues every LSEG/Nasdaq catalog year 2016–2026 plus known official Nasdaq annual releases. It is a finite discovery inventory, not exhaustive historical coverage. `IndexRequest` is immutable; `PublicIndexClient.fetch(request, timeout_seconds=...)` returns `IndexResponse(content, status_code, captured_at, content_type)`. The collector-created client shares the existing exact `{source}:shared_account` rate-budget key with supplemental collection; injected clients must preserve that budget contract. The transport uses unauthenticated HTTPS, bounded streamed bytes and timeouts, no redirects, and only a published LSEG read-only catalog POST. No credentials/configuration are loaded.

`index_history_record` references the existing `supplemental_document`, which references source contract and hash-addressed raw bytes. Each record preserves `record_kind`, source index namespace, source ticker/name, action, announcement day, publication day, effective day/phase, snapshot day, weight day/unit, exact Decimal weight, capture timestamp, source fields and issues. Source tickers are not joined to current security identities. `canonical_eligible` is always false and enforced by SQL; the evidence table rejects updates/deletes.

`index_acquisition_state` stores the original full manifest hash/count/window and durable request cursor. A PostgreSQL advisory lock protects one job. Raw document registration, record inserts and page advancement commit together. A failed SQL page leaves the queue unchanged. Denied/unrecognized responses are archived with explicit `access_gap` records. A Nasdaq access denial stops repeated requests to that host for that job. Queue completion always returns `coverage_complete=False`. Projection uses separate per-document completion state, so newly appended supplemental documents can be discovered even when their IDs sort before earlier documents.

For portable transfer, register `index_history_record` with its `supplemental_document` FK and existing document/raw/contract dependencies. Acquisition state is operational state: transfer only under an explicit fresh remote authority and preserve the full target manifest; do not replace a live local cursor. This package does not change the shared remote registry or activate remote execution.

`membership_from_records(records, index_key=..., on=..., as_of=...)` inspects an exact, provider-declared complete snapshot only. It requires source publication metadata and a matching complete member count, rejects a future snapshot, returns unavailable without a baseline, and never reconstructs historical membership from a change-only list. It suppresses weights whose date differs from the snapshot day. Even a valid source snapshot does not establish continuous historical coverage or canonical security identity.

## Actual public downloads verified 2026-10-10

Every row below was fetched with an actual unauthenticated HTTP GET and parsed from its complete PDF. All ten responses were HTTP 200. Counts are securities in that particular published change list, not complete index membership counts. The effective-day/phase for June reconstitution comes from the [2025 official schedule](https://www.lseg.com/en/media-centre/press-releases/ftse-russell/2025/ftse-russell-begins-37th-annual-russell-reconstitution) and [2026 official reconstitution page](https://www.lseg.com/en/ftse-russell/russell-reconstitution). Publication dates remain null: the file-date label, filename, copyright year, and `Data as at` date are not silently promoted to publication.

| Official download | Bytes | Parsed changes | Effective day / phase |
|---|---:|---:|---|
| [Russell 3000 2025 additions](https://www.lseg.com/content/dam/ftse-russell/en_us/documents/other/ru3000-additions-20250627.pdf) | 213,875 | 228 additions | 2025-06-27 / after close |
| [Russell 3000 2025 deletions](https://www.lseg.com/content/dam/ftse-russell/en_us/documents/other/ru3000-deletions-20250627.pdf) | 177,361 | 152 deletions | 2025-06-27 / after close |
| [Russell 3000 2026 additions](https://www.lseg.com/content/dam/ftse-russell/en_us/documents/other/ru3000-additions-20260626.pdf) | 976,799 | 224 additions | 2026-06-26 / after close |
| [Russell 3000 2026 deletions](https://www.lseg.com/content/dam/ftse-russell/en_us/documents/other/ru3000-deletions-20260626.pdf) | 1,010,529 | 118 deletions | 2026-06-26 / after close |
| [Russell Microcap 2026 additions](https://www.lseg.com/content/dam/ftse-russell/en_us/documents/other/rmicro-additions-20260626.pdf) | 980,580 | 252 additions | 2026-06-26 / after close |
| [Russell Microcap 2026 deletions](https://www.lseg.com/content/dam/ftse-russell/en_us/documents/other/rmicro-deletions-20260626.pdf) | 970,159 | 154 deletions | 2026-06-26 / after close |
| [Russell 1000 September 2026 IPO additions](https://www.lseg.com/content/dam/ftse-russell/en_us/documents/other/final-ipo-additions-3-qtr-r1000.pdf) | 413,445 | 5 additions | 2026-09-21 / phase unknown |
| [Russell 2000 September 2026 IPO additions](https://www.lseg.com/content/dam/ftse-russell/en_us/documents/other/final-ipo-additions-3-qtr-r2000.pdf) | 418,260 | 28 additions | 2026-09-21 / phase unknown |
| [Russell 3000 September 2026 IPO additions](https://www.lseg.com/content/dam/ftse-russell/en_us/documents/other/final-ipo-additions-3-qtr-r3000.pdf) | 419,704 | 33 additions | 2026-09-21 / phase unknown |
| [Russell Microcap September 2026 IPO additions](https://www.lseg.com/content/dam/ftse-russell/en_us/documents/other/final-ipo-additions-3-qtr-rmicro.pdf) | 415,818 | 18 additions | 2026-09-21 / phase unknown |

Total parsed: 1,212 change records. The IPO resources are linked from the [official quarterly IPO page](https://www.lseg.com/en/ftse-russell/russell-reconstitution/ipo-additions); their PDF heading states the effective day. The original source as-at day, where text extraction exposes it, is retained independently. No weights occur in these tables. The PDF parser dependency `pypdf==6.11.0` is declared in `pyproject.toml` and installed in the local verification environment. If unavailable on another host, the acquired response is staged as a parser/access gap rather than reported as successful membership acquisition. Production SQL readback is recorded in `artifacts/index-history/production-verification.json`; the original full scope is retained and historical coverage remains incomplete.

The 2025 additions download was also fetched and stored in the guarded PostgreSQL `_test` database, then queried back as 228 immutable change records. SHA256 `83762a06322c86c6faa8d571f5ab571fb0ba654467384c1450cede7ea01fa4fb` and 213,875 raw bytes matched the hash-addressed gzip archive. This is an isolated test, not a production collection claim.

## Catalogs, historical notices and access gaps

| Source | Reachable material / exact fields | Remaining gap |
|---|---|---|
| LSEG official technical-notice catalog | Published JSON POST; `Data`, `TotalCount`, `id`, `FamilyMnemonic`, `AnnouncedDate`, provider-spelled **`ElectiveDate`**, `title`, `url`. Existing supplemental captures can be projected without fetching again. | Catalog metadata is an announcement, not a membership baseline or constituent-weight file. Exact index identity may remain unresolved at the family level; full notices have not been obtained for every record. |
| Nasdaq official historical annual releases | Official web-indexed 2016–2021 annual releases provide header/dateline publication day, before-open effective day and add/remove ticker expressions. The parser splits multiple source security classes such as `LBTYA/LBTYK`, without assuming equal company/security counts. | Actual direct 2016 HTML, search catalog and known static PDF GETs returned HTTP 403. No bypass/login/repeated denial probing. Historical intra-year changes and 2022–2026 release inventory remain incomplete. |
| Nasdaq full weighting | [Official NDX weighting page](https://indexes.nasdaqomx.com/Index/Weighting/NDX) requests login/register for full access. | No public complete historical weights/baseline obtained. Future pro-forma portfolios cannot substitute for historical actual membership. |
| S&P Dow Jones Indices | Existing official S&P press-release table captures project into SP500 / SP400 / SP600 change records, preserving announcement and after-close effective day. [A verified 2016 release](https://press.spglobal.com/2016-01-22-Federal-Realty-Investment-Trust-Set-to-Join-the-S-P-500-Curtiss-Wright-Pool-to-Join-S-P-MidCap-400-Cray-Asbury-Automotive-to-Join-S-P-SmallCap-600) is supported. | Company names without source security identifiers stay unresolved. No full historical baseline, complete change catalog, or weights obtained; an RSS probe was denied in earlier discovery. |
| KRX | The [official index site](https://index.krx.co.kr/main/main.jsp) exposes composition/history navigation and change-notice titles. The composition URL `contents/MKD/03/0304/03040101/MKD03040101T3.jsp?idxCd=1028&upmidCd=0102` rendered an access-denied page. | No verified public composition/history API, downloadable baseline or historical weights obtained. Approved KRX OpenAPI index-daily-value endpoints do not contain membership. This package does not invent an API, use proxy holdings, or bypass access controls. |
| Older Russell files | A primary-source-indexed [2023 final deletions URL](https://content.ftserussell.com/sites/default/files/ru3000_deletions_final_20230623.pdf) returned HTTP 301 in direct GET. | Redirect destination was not followed or treated as acquired content. 2016–2024 complete membership/weight history remains missing. |

Free public acquisition remains partial. If complete historical baselines or weights require licensed provider delivery, acquisition needs an independently authorized official file/API contract; this code does not purchase, log in, or claim that a publicly downloadable change list resolves that gap.

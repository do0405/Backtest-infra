# Additional historical evidence

The full target remains KR4,507 + US13,251 = 17,758, 2016-01-01..2026-10-07.
Source availability, an audit pass and a few saved observations do not certify
complete market coverage.

| Gap | Additional official source | Free scope and limitation |
| --- | --- | --- |
| US holidays and early closes | [Alpaca calendar](https://docs.alpaca.markets/us/reference/legacycalendar) | Actual HTTP200 and SQL/raw capture of 2,706 open days for the requested full interval on 2026-10-11. Broker declaration, not exchange certification. |
| Historical OTC symbol/name/listing events | [FINRA OTC Daily List](https://otce.finra.org/otce/dailyList) | Official public OTC event information. It does not supply all NYSE/Nasdaq historical eligibility; symbols alone do not prove reused-security identity. |
| Exchange-listed US delistings | [NYSE notices](https://www.nyse.com/regulation/delistings?market=NYSE), [NYSE Market Event Feed](https://www.nyse.com/market-data/corporate-actions/market-event-feed) | Public notices can add evidence; the historical event feed offers purchased access. A Form25 filing date is not automatically the last trading/effective date. |
| Nasdaq historical corporate-action and identifier events | [Nasdaq Daily List specification](https://nasdaqtrader.com/content/technicalSupport/specifications/dataproducts/dlcompletespec.pdf) | Documented official event feed; full access depends on supplied entitlement. No credentials, subscription or purchase has been added. |
| Fund holdings before N-PORT | [SEC N-Q](https://www.sec.gov/about/forms/formn-q.pdf) and N-CSR filings | Free historical EDGAR documents can supplement the earlier years. Structured parser/source bindings are still needed; report date and public filing availability remain separate. |
| Later US fund holdings | [SEC N-PORT datasets](https://www.sec.gov/data-research/sec-markets-data/form-n-port-data-sets) | Free quarterly bulk files cover the N-PORT era. These are fund holdings, not index weights, and do not fill all earlier dates or ETNs. |
| Korean listing/delisting and identifier changes | KRX/KIND official listing notices plus existing full-ISIN basic information | Preserve explicit notice/effective dates and the exact security binding. Current basic information and a reported listing date are source declarations; complete historical lifecycles are not inferred. |
| Full index baseline/weights | Official index provider snapshots and change announcements | Existing Russell PDFs/notices add events. An announcement sequence without an initial baseline and weights cannot reconstruct a complete history. ETF holdings are labeled proxies, never exact index weights. |

Only the calendar addition above has new actual API/SQL verification in this
implementation. The other entries are source candidates, not claims that new
collector connections or paid historical access are complete. Existing approved
supplemental collection continues independently. Login, access changes and paid
subscriptions have not been performed.

Coverage eligibility is an append-only overlay with its own evidence cutoff and
source references. The original provisional weekday audit, target hash, prices,
capture clocks and original denominator remain intact. The b918 production
migration/overlay must follow the already running b916/b917 index work; the new
tables and semantics have been tested in isolated PostgreSQL without rerunning
the production migrations during those builds.

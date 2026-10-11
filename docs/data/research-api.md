# Local research API

The loopback server uses port 8765 and serves the built React application. API
inputs are bounded and registered; there is no code or SQL upload.

* `GET /api/securities?query=AAPL&market=US&limit=20` returns security_id, name,
  market, identifier and historical_identity. Search is within the original target.
* `GET /api/series?security_id=UUID&start=2020-01-01&end=2026-10-07` returns full
  source series identities with series_key, source/basis/adjustments/volume_basis.
* `POST /api/datasets`: selections (one to twenty series), start, end, cutoff.
  Each selection contains security_id, source_contract_id, basis, feed, venue,
  trade_scope, currency, quantity_unit and series_key. Additional response fields
  from the series endpoint must be omitted. The response has exact Decimal-string
  bars with nested series, sessions, dataset_id, hashes, origin, calendar_grade,
  quality_gaps and original_target_count=17758. It is an immutable bounded source
  snapshot, not a frozen full-universe release.
* `GET /api/features?dataset_id=HASH&series_key=HASH&sma_period=20&rsi_period=2`
  returns rows of session,price,sma,rsi,volatility,momentum,simple_return,statuses.
* `GET /api/publications?security_id=UUID&cutoff=ISO` shows recent exact-publication
  metadata assertions with public_day/effective_available_at/source_version_ref.
  It does not replace a financial ratio/revision query.
* `POST /api/backtests`: dataset_id,raw_series_keys,optional feature_series_keys,
  strategy (trend_sma,mean_reversion_rsi2,price_momentum), parameters, initial_cash,
  fee_bps,slippage_bps. Uses actual Backtrader. Result has immutable result_id,
  metrics, NAV rows, ledger, decisions and per-security selection_trace. Candidate
  selection, pending orders, actual fills and closing holdings remain separate.
* `GET /api/results/HASH`, `POST /api/results/HASH/replay`: exact saved result and
  repeat execution against its immutable input. Code/input mismatch fails replay.
* `GET /api/results/HASH/analysis?benchmark_series_key=HASH`: optional explicit
  saved-input series for a reproducible post-run report. The report binds the
  original result/input and reporting code with its own analysis_id. It includes
  FIFO closed portions, remaining lots, realized net PnL/win rate/profit factor,
  Sortino/Calmar, drawdown duration and exposure measures. An omitted benchmark
  is not replaced with an arbitrary security or index.
* `GET /api/results/HASH/export?format=result|dataset|analysis|decisions|ledger|closed_portions`:
  attachment download of exact result/input/analysis JSON or UTF-8 CSV. Optional
  benchmark_series_key applies to the analysis JSON. Every CSV row retains
  result_id/dataset_id; compound trace evidence remains a JSON cell. Formula-like
  text is prefixed with an apostrophe, while signed numerical strings are retained.
  All downloads verify the saved result and perform no SQL or provider calls.
* `POST /api/screen`: dataset_id,basis,feature,top_n returns stable selected subset
  rows and explicit exclusions. Original target is retained; this is not a
  historically complete universe screen.

KR and US currencies run separately. Unknown corporate-action coverage, model
costs, liquidity and tax assumptions are visible in each result. Calendar gaps
are never compressed. A provider calendar is labeled as a broker declaration.

The optional benchmark normalizes one declared close series to the first strategy
NAV on exactly the saved session grid. It is a zero-cost price comparison, not
a modeled buy-and-hold fill, total-return index or independent provider consensus.
The strategy retains its modeled fees/slippage. Basis/adjustments/source remain
visible. Missing benchmark dates are null without carrying/compression; the
aggregate relative return is unavailable when any benchmark session is missing
or the strategy carries stale marks. Same market/currency is required.

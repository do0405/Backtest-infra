# Research follow-up within the approved daily research scope

The user asked to complete useful remaining research features while remote operation is connected separately. Existing datasets, result hashes, source rows and collector ownership remain unchanged. This work has no provider, operational SQL or server restart steps.

1. Add a derived, content-addressed analysis report over an existing verified result and its exact stored dataset. Attribute FIFO closed portions, entry/exit fees, taxes, realized gains/losses and still-open quantities. Do not count open lots as wins or subtract slippage twice from already slipped execution prices.
   Add descriptive zero-target Sortino, elapsed-day CAGR/Calmar, drawdown duration in calendar days and equal-declared-session exposure measures, with missing/zero denominators explicit.
2. Allow the user to choose one explicitly identified dataset series as a normalized close-price benchmark. Require the same market/currency and exact result session grid. Preserve gaps, source/basis, calendar grade and observation references. A normalized price comparison has zero costs and is not a modeled buy-and-hold execution or dividend total return. Missing sessions or stale strategy marks make the aggregate comparison unavailable.
3. Add verified result JSON and decision/ledger/closed-portion CSV downloads. JSON preserves exact values and original identifiers; CSV includes references and protects spreadsheet formula cells. Downloads are read-only and do not overwrite saved results.
4. Display comparison, trade statistics, closed portions, open quantities and download links in the existing research screen. Handle report failure independently of the saved result, and clear stale analysis when changing a result or benchmark.
5. Validate hand-computed partial exits and costs, no-loss/no-trade denominators, incompatible benchmarks, missing grids, deterministic report hashing, API downloads, CSV formula safety and frontend state/labels. Run the existing research regression and frontend build.

Remaining evidence-dependent scope: dividends/splits/delisting proceeds, tax and liquidity rules, verified historical universes/index weights, official KR lifecycle/calendar, mixed-currency valuation and financial-factor projection are not invented from existing price data. They need separate admission and execution contracts.

## Implemented and verified

All five implementation steps are complete in source. The report/API/UI-only
extension retains original framework fingerprints and immutable saved results.
The full existing offline research regression plus new report checks passed 49
tests; after pinning Decimal precision and rounding independently of the caller,
the changed report/API/framework subset passed 29 tests again. The frontend passed
20 checks in nine files and built 29 modules. The copied static build and tested
source/configuration hashes are recorded in `frontend/build-receipt.json`.

The actual stored Samsung result was read without SQL/provider access. Seven
analysis/export requests returned HTTP200 through the production API code in a
local test application: decision CSV27 rows, fill CSV3 rows, closed-portion CSV1
row. Exact result JSON and same-input Backtrader replay retained the original
result_id. Its price benchmark keeps the three missing provisional calendar
dates and returns an unavailable aggregate comparison. This is actual saved
source evidence, not a fixture substituted for SQL failures. Live server restart
and browser publication verification remain the parent's operational scope.

Independent review then reproduced and fixed two counterexamples in report-only
code: finite price data whose annualized CAGR overflows, and a missing initial
benchmark anchor falsely listing all later observed dates as price gaps. The
new numeric policy preserves finite ordinary returns and emits explicit null
reasons for unrepresentable derived metrics, without changing engine evaluation.
Actual absent observations and normalization-unavailable sessions are separate;
the UI explains their difference. Additional API/handcase/UI regressions were
added and the complete frontend now passes 21 tests in nine files.
The final report/API/actual-framework subset passed 33 checks. A separate
nonfinite-float/Calmar counterexample is covered, in addition to HTTP200 for the
valid high-growth benchmark and the initial-anchor distinction. The actual saved
Samsung result again produced HTTP200 and the same original Backtrader replay ID.

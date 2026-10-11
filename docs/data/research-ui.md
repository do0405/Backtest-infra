# Local research workspace

The Korean React interface is served by the loopback research API at
`http://127.0.0.1:8765/`. It uses relative, same-origin API requests, locally
bundled React and Lightweight Charts 5.2.1, with no remote fonts, scripts,
credentials or production order routing.

## Build and verify

From `frontend`, use `npm ci`, `npm test`, and `npm run build`. Exact direct
dependency versions and the full dependency tree are pinned by package.json and
package-lock.json. The Python server mounts frontend/dist when that directory
exists at startup. Restart the research server after the first build if its
application was created before dist existed. The Vite development proxy rewrites
the backend Host/Origin to the loopback API; the deployed page uses same origin.

On slow external disks, install and build a copy of the small frontend source in
a local temporary directory. Copy only source, tests, lockfile and dist back;
do not copy node_modules. build-receipt.json records SHA-256 equality of the
tested source and built files copied back from the temporary workspace.

## Research flow

1. Pick a bounded date range and an ISO cutoff containing its timezone.
2. Search within the original 17,758-security target. Select each full price
   source identity explicitly; a single dataset uses one market and currency.
3. Load an immutable actual-source snapshot. Fixture or empty snapshots fail.
4. Inspect the real candlestick chart, source/basis/volume/currency information,
   declared calendar, quality gaps, publication metadata, and exact feature
   values. Chart gaps retain the declared date grid. SMA is configurable; RSI
   uses 2 sessions; momentum uses the documented 252/21 default.
5. For each raw execution series, explicitly select a feature source from the
   same security. Raw and provider-adjusted sources are never paired silently.
   Select trend SMA, RSI mean reversion or price momentum, their registered
   parameters, cash, fee and slippage assumptions, and run the actual framework.
6. Review NAV/drawdown, raw metric values, ledger and the date selector. Click a
   chart date or use keyboard-accessible previous/next/date controls. Candidate,
   desired target, pending instruction, same-session fill and closing holding
   stages use separate authoritative trace fields. Numerical strings are
   preserved exactly in tables; finite Number conversion is confined to chart
   data. Metric ratios retain the backend's raw values.
7. Expand trace evidence to see required feature values/statuses, compact
   boundary references, and the full observation catalog filtered by each
   inclusive feature input window. Missing declared observations remain absent;
   boundary references do not masquerade as complete lineage.
8. Run a stable screen of the chosen subset at its last declared session. Basis,
   ranked scores, eligible/excluded counts and explicit exclusion reasons remain
   visible. It is not a verified historical whole-universe screen.
9. Inspect FIFO closed portions separately from open positions. Select a price
   benchmark explicitly from the same saved dataset/market/currency. The line
   is normalized at the first strategy NAV and has zero costs; it is not a
   dividend total return or modeled buy-and-hold execution. Missing benchmark
   sessions remain gaps and block aggregate relative return; carried strategy
   marks also block that comparison. Sortino/Calmar and exposure assumptions are
   available alongside the original metrics.
10. Download result/input/analysis JSON and decision/selection, fill and closed
    portion CSV. Files retain original result/data identifiers and exact values;
    downloads do not change saved results or call the providers.

Results retain the original target, immutable input/result identity, engine and
code fingerprints, date conventions, modeling assumptions and data-quality
limitations. No fixture, alternate engine or fabricated chart is substituted
when SQL, the API or the qualified engine fails.

## Saved actual results and browser verification

Open `/?result=<64-lowercase-hex-result-id>` to load an existing saved result.
The interface fetches `/api/results/<id>`, validates actual framework execution,
then retrieves `/api/datasets/<dataset_id>`. It restores explicit source choices
and run parameters and resolves names by exact security ID within the original
target. A failed name lookup leaves its actual security ID visible.

The verified actual Samsung research sample supplied during implementation is:

`http://127.0.0.1:8765/?result=fbb3f68fbb5517e396ca902a9999240875329c963dfb02a8ecfbfd9111fdeefd`

Its existing immutable dataset is
`fec8bf374ef95e01aa5042308eaedbcb462f0eef9017f2988144ac0f373acb0b`.
This URL is a reference to an existing actual result, not bundled demo data.
The page has no preloaded sample by default. The result can also be opened from
the saved-result ID input and replayed against the same immutable input.

## Validation

Twenty meaningful frontend tests cover exact numerical strings, chart admission of
nonfinite/malformed values and explicit date gaps, distinct trace stage filtering,
full-window versus boundary input references, strict selection payloads and
explicit feature mapping, API/network/fixture/empty-input failures, and restoring
dataset B after screening dataset A without retaining A's ranking, and saved
selection labels updating when security names resolve later, and the chart size
boundary through repeated resize feedback and selection changes, and data-table
CSS padding/border isolation from chart-owned internal tables. Snapshot
changes clear every derived view, including screening, features, publication
metadata, active series and selected date. The
Chart containers have independent fixed heights: 320 px for price/NAV and 180 px
for drawdown. Auto-size observes responsive width within that boundary; padding
and attribution remain outside the observed container. Application table
selectors are scoped to `.table-wrap`, preserving the chart renderer's internal
table sizing. The production build
uses the current v5 chart API (`addSeries` and
`createSeriesMarkers`). Chart attribution links to TradingView.

The four report/benchmark follow-up checks cover explicit benchmark selection,
source/cost distinctions, immutable download links, result identity validation
and stale report clearing, and NAV comparison lines preserving null sessions
without affecting ordinary price candles. Report loading errors leave the
original saved result, ledger and trace accessible.

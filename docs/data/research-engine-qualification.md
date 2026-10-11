# Research engine qualification

The exact `AnalysisDataset` is the boundary between source projection and research.
Features, strategy decisions, evaluation, screening and result storage have no
provider, database, HTTP or order-routing imports. The original 17,758-member target
and manifest hashes remain in each bounded result. A subset is not a verified
historical universe. Performance is explicitly in-sample research.

## Framework selection and execution gate

Zipline-reloaded 3.1.1 was the first candidate. A Windows CPython 3.13 wheel is
available, but its `bcolz-zipline` dependency requires a native build that failed
because Microsoft Visual C++ 14 was absent. The separate temporary environment has
not qualified Zipline. Import success alone would not qualify it. Explicit
Zipline selection raises `EngineUnavailable`; there is no internal accounting
engine fallback. The original ingestion environment has not been changed.

Backtrader 1.9.78.123 is installed in the separate research environment and the
`engine='backtrader'` adapter has passed actual ledger qualification for
the restricted scope below. **Backtrader is now the selected default** for the
user's performance and session-by-session security-selection workflow. Its
[next-open cycle](https://www.backtrader.com/docu/cerebro/cheat-on-open/cheat-on-open/)
allows previous-close decisions and current-open integer sizing before the
framework broker executes orders. The [broker](https://www.backtrader.com/docu/broker/)
owns cash, positions, fees and valuation. The adapter creates own-data feeds from
the supplied dataset and calls `Cerebro.run`; no custom accounting loop or oracle
is substituted for the framework. Feature decisions happen at the prior close;
the separate open callback reads only raw open prices for sizing. Positive daily
volume is an explicitly labeled ex-post fill gate.

## Current capability matrix

| Capability | Qualification status |
| --- | --- |
| Declared-basis SMA, Wilder RSI, returns, log-return volatility, 252/21 momentum | Offline hand-value and prefix checks implemented |
| Generic strategy extension and trend/RSI/momentum plugins | Extension/state, momentum ties/exclusions and periodic rotation passed |
| Stable subset ranking and explicit exclusions | Offline tie, missing-grid and basis checks implemented |
| Immutable result integrity and replay input binding | Hash/store/fault checks and exact framework replay passed |
| Framework buy, sell, raw marks, integer sizing, cash shortage, costs | Backtrader actual execution and hand-ledger parity passed |
| Simultaneous sell-before-buy | Backtrader two-asset rotation and cash/NAV parity passed |
| Missing open/bar, zero volume, known halt | No-fill/pending tests passed; malformed zero open fails exact bar admission |
| Missing held-position mark | Explicit last-raw-close carrying with stale-mark flag passed; never used to fill |
| RSI maximum holding interval | Actual fill-day start and eligible-session counting passed; zero-volume session excluded |
| Stateful extension | Registered plugin execution and past-state prefix invariance passed |
| Per-session security selection trace | Required features/statuses, ranking, targets, queues, fills, holdings and observation lineage passed |
| Official unknown tradability | Only declared `research_daily_tradability_v1` may run; strict path rejects |
| Split, reverse split, cash dividend, delisting proceeds | Unsupported; reject supplied actions before framework execution |
| Corporate-action coverage unknown | Declared price-only research only; never cash-dividend total return |
| Mixed market/currency portfolio | Rejected without verified FX and compatible valuation times |
| Official market calendar certification | Dataset grade retained; observed session union remains uncertified |
| Short, margin, fractional shares, intraday stops/limits, cash interest | Unsupported |

The fixture adapter uses the supplied declared KR/US session grid. It retains
`calendar_grade`; it never certifies an observed union as an official calendar.
Known halt entries are dated security identities supplied in run parameters.
Official status remains unknown for other observations. The volume gate does not
provide an auction participation or liquidity-cap model; a positive daily volume
is not proof the entire modeled quantity could execute at the opening auction.
Those liquidity constraints remain unqualified. The initial cost profile uses
explicit fee and slippage bps, with tax explicitly unmodeled/zero. It is not a
broker-cost or tax-rule certification.

## Momentum selection and rebalance definition

The `price_momentum` plugin ranks only the explicitly selected dataset subset.
Default parameters are `momentum_lookback=252`, `momentum_skip=21`, `top_n=20`,
`rebalance_every=21`, `positive_only=true`, and `allocation=1`. Available positive
momentum scores rank descending, with ties resolved by stable security ID and
then raw series key. Missing sessions and warmup remain explicit exclusions.
Each selected candidate receives `allocation/top_n`, even if fewer candidates
are eligible, so unused slots remain cash. Every supplied feature series retains
its declared basis; raw momentum is not called split-adjusted momentum.

Rebalancing occurs at declared grid indices 0, N, 2N, etc. It is a fixed session
cadence, not a claim of calendar month-end rebalancing. The close decides targets;
the next eligible raw open handles integer sizing, sells before buys, cash and
costs through Backtrader. A scheduled rebalance can requeue an unchanged weight
to resize it. Between rebalance closes, current rankings are still displayed but
the previous targets remain active. This anchor is explicit and prefix invariant.

## Selection trace JSON contract

`selection_trace` is a flat array ordered by session and stable security ID.
`decisions[i].selection_trace` contains the same rows for that close. Each trace
row has these fields:

| Field | JSON type and meaning |
| --- | --- |
| `session`, `security_id`, `raw_series_key`, `feature_series_key` | Strings identifying the close and separate source series |
| `feature_values` | Mapping of required feature names to exact numeric strings, float volatility, or null |
| `feature_statuses` | Required feature name → `{status, reason}`; unavailable values retain warmup/gap reasons |
| `eligible`, `exclusion_reason`, `rank`, `score` | Current ranking eligibility; boolean, nullable reason, nullable integer, nullable numeric string |
| `candidate_selected` | Boolean indicating current top-N/rule candidacy, independent of the rebalance schedule |
| `target_weight`, `selected` | Numeric string desired weight and whether it is positive after this close's decision |
| `rebalance_due` | Boolean for the momentum schedule; null for other plugins |
| `decision_reasons` | Per-security reason strings, including exclusions or holding the periodic target |
| `pending_target` | Null or `{status, signal_session, target_weight}`; status is `queued_next_open` or `pending_unfilled` |
| `actual_fills` | Same-session framework ledger records; earlier decisions are never updated with future fills |
| `execution_events` | Same-session blocked/shortage/rejection records for that security |
| `actual_holding_quantity`, `actual_held_sessions` | Integer closing framework position and eligible holding-session count |
| `actual_holding_weight` | Numeric string actual marked exposure/NAV, or null for invalid NAV |
| `input_observation_refs` | Boundary/current observations with series key, session, observation/input hashes and raw/decision IDs |
| `input_reference_scope` | `window_boundaries_and_current_observation`; refs are explicitly a compact boundary view |
| `feature_input_windows` | Per-required-feature series key, start/end session, required initial sessions, grid/observation counts and full-window reference hash |

`input_observation_catalog` records each selected raw/feature observation once,
including captured and available timestamps. Full feature provenance is the
catalog filtered by the trace window's series and inclusive session bounds;
missing grid observations remain absent. `input_refs_hash` is the canonical hash
of those chronological observation refs without captured/available timestamps.
For Wilder RSI the window runs from the last gap through the current session,
covering its recurrence history. The compact boundary refs never replace the
full-window lineage. `selection_trace_policy` records the time conventions,
subset scope, rank policy, cadence anchor and allocation slots.

`selected` and `candidate_selected` are strategy facts. Neither says that a buy
filled. `pending_target`, `actual_fills` and `actual_holding_quantity` provide the
separate execution and holdings facts. The independent `screen_subset` output
remains a candidate report and does not modify backtest holdings.

## Calculation and numeric policy

Prices and lineage remain exact `Decimal` inputs and are hashed before any engine
conversion. Price arithmetic uses a pinned Decimal context of precision 28;
nonterminating ratios are rounded by that declared context. Feature means use
exact contiguous declared-session counts. Wilder RSI initializes with the mean
of the first N changes and updates with alpha 1/N. Zero loss with positive gain
means 100, and zero gain plus zero loss means 50. Missing declared sessions reset
RSI initialization and interrupt every required trailing window. Calendar gaps
are never compressed. Momentum is P[t-21]/P[t-252]-1 by default, using session
offsets, not calendar-month approximations disguised as exact months. Volatility
uses 20 log returns, sample standard deviation (ddof 1) and sqrt(252).

Raw features identify themselves as `raw_as_traded`. Provider-adjusted data is
called split-adjusted only when its declared adjustments are exactly `('split',)`.
Feature prices can differ from raw execution/mark prices through separately
selected full series identities. Neither a provider label nor matching ticker
proves split coverage.

Float64 engine admission validates finite conversion and rejects underflow or
an error greater than max(1e-12 absolute, 1e-12 relative). It does not change the
input value, source identity, content hash or stored raw reference. Float log/std
features are compared at 1e-12 relative/absolute tolerance. A qualified framework
ledger must match hand cash/NAV and fill quantities, with monetary differences
below 1e-8 absolute or 1e-12 relative. Test tolerances are fixed before comparison.

CAGR uses elapsed days and 365.2425 days per year. Drawdown uses NAV/running peak-1.
Turnover sums gross traded notional divided by previous-session NAV. Costs retain
fee, slippage and tax separately. Missing/nonpositive denominators produce null
with a reason. The initial Sharpe risk-free assumption is zero and annualization
is 252 sessions. FIFO closed-lot portions include allocated entry/exit fees and
tax; still-open portions do not enter trade win rate.

## Verification

`tests/oracles/research_ledger.py` contains fixture generation and independently
hand-computed values only. Production modules do not import it. Offline checks
are in the four `test_research_*` unit modules. The framework qualification suite
is `tests/integration/test_research_engine.py` and requires the selected pinned
framework environment, not PostgreSQL. Its pass/fail receipt must be recorded
before changing any matrix row to qualified.

Qualification receipt on 2026-10-11: Python 3.13.13, Backtrader 1.9.78.123,
dedicated `C:/Users/pc01/AppData/Local/Temp/backtest-research-20261011` runtime.
The four unit modules plus offline framework suite passed **31 tests**. Backtrader
emitted 308 upstream `datetime.utcnow()` deprecation warnings; there were no test
failures or skips. No PostgreSQL, providers, production migrations or collectors
were used by this qualification.

Hand ledger: closes 10,12,16,8,8; corresponding opens 10,12,15,8,8; initial cash
100; fast SMA1 versus SMA2. The decision after the second close buys 6 integer
shares at the next raw open 15. Cash becomes 10 and NAV 106. The decline produces
a cash decision; 6 shares sell at raw open 8, leaving cash/NAV 58. Framework NAV
is exactly 100,100,106,58,58 for this fixture. With fee and slippage both 100 bps,
framework execution prices are 15.15 and 7.92, fees 0.909 and 0.4752, final cash
55.2358 within the predeclared float tolerance. A same-session two-asset rotation
also sells A before using the framework cash proceeds to buy B.

## Read-only post-run analysis

`evaluation/reporting.py` produces a separate content-addressed analysis over a
verified saved result and its exact dataset. It does not replace the framework,
modify original results or enter the engine code fingerprint. Original same-code
replay remains compatible with this reporting/API/UI-only extension.

FIFO attribution records entry/exit ledger indices/sequences, quantities, prices,
allocated entry/exit fees and tax, gross/net profit and calendar holding days.
Partial exits are closed entry-lot portions, not whole position episodes. The
final portion receives the remaining fee/tax balance so repeated fractions do
not discard cost. Open quantities and residual entry cost remain separate.
Slippage is already reflected in execution prices and is not subtracted twice.
Profit factor has no value without realized losses; no closed portion gives a
null win rate rather than a manufactured zero.

Sortino uses a zero per-session target and downside squares divided by all
declared session returns, annualized with sqrt(252). Calmar divides elapsed-day
CAGR by absolute maximum drawdown. Zero downside/drawdown and missing exposures
have explicit null reasons. Drawdown duration is calendar days from the most
recent equal-or-higher peak through recovery or the final session. Exposure
means/fractions weight declared grid sessions equally. These are descriptive
in-sample metrics; provisional calendars and carried marks retain their flags.

The explicit benchmark is a normalized declared close-price series with zero
costs and no execution model. Its initial value equals the first strategy NAV.
Original market/currency, exact date grid, basis/adjustments and observation refs
are retained. Missing dates remain null. Any benchmark gap or strategy carried
mark blocks aggregate relative performance. This does not implement a dividend
total-return benchmark or certify an ETF proxy as its underlying index.

Additional scope still requires separate contracts/evidence: versioned broker
tax/spread/minimum-fee and decision-time liquidity caps; action/dividend/delisting
cash/share processing; FX and asynchronous market valuation; financial-factor
step functions; registry-backed OOS/walk-forward trial/holdout history; verified
historical index/member weights and lifecycle. Nothing in this report certifies
those missing capabilities from partial price data.

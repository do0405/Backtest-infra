# Supplier price declarations and comparisons

The `supplier_price_reconciliation_v2` policy preserves supplier OHLC values and nominates an existing observation. It never averages prices, reconstructs an adjusted price from actions, changes a source revision, or certifies VWAP. Corporate actions remain supplemental evidence. The strict price validation, action-ledger selection, and release policies retain their existing behavior. Existing v1 audit receipts remain immutable.

One structurally valid, completed-session price with a registered API adjustment declaration may be used with the `supplier_declared` grade. Missing action history or a historical adjustment reference does not invalidate that declaration. Its separate corroboration grade is `not_corroborated`.

A comparison requires the same security, session, currency, price/quantity units, feed, venue, session scope, trade eligibility scope, adjustment definition and adjusted-price reference basis. An unknown scope or adjusted reference remains incomparable even if the numbers match. KIS's unspecified provider actions differ from Alpaca's split-only definition. A capture timestamp records acquisition; it is never substituted for an adjustment reference.

OHLC agreement uses an explicitly evidenced absolute tick tolerance or the sum of half of both source rounding quanta, whichever is larger. Exact equality does not require a rounding allowance. Differences without a verified tolerance remain unresolved. Decimal operations ignore ambient precision. Quantity comparison is optional in the pure pair interface; SQL reconciliation compares OHLC only and records `quantity_corroborated=false` and `matched_vwap=false`.

A unique agreeing group nominates an existing value by `source_priority`, then a deterministic source/contract/observation ordering. An explicit `candidate_id` can choose a member of that group. Two agreeing suppliers among three can qualify the nomination; the dissenting observation and comparison issues remain in the audit. Two disagreeing suppliers, disjoint agreeing groups, overlapping nontransitive agreement, or conflicting same-clock revisions are quarantined. An explicit candidate among incomparable peers can receive a declaration-only nomination. A quarantined comparison does not rewrite earlier supplier declaration decisions.

Corroboration grades distinguish `agreement_independence_unknown`, `agreement_shared_provenance`, and `corroborated_independent_paths`. Different provider names or contracts do not prove independence. A verified comparison contract plus explicit upstream and processing-path evidence is required for the strongest grade; shared upstream processing is identified in each pair. Selection decisions retain the supplier declaration grade, while the separate corroboration grade, dissent, source IDs, original clocks and exact values are stored in the validation audit.

The pure interfaces are `compare_supplier_prices(left, right, *, as_of, contract, compare_volume=False)` and `reconcile_supplier_prices(candidates, *, as_of, recorded_at, contract, candidate_id=None, source_priority=())` in `prices/reconciliation.py`. Candidates carry explicit comparison metadata. At most 64 unique candidates are accepted. Current revisions are the latest supplied captures per source contract, semantic context and declared adjustment reference. Price capture PIT is not required: an October 7 US price captured October 10 is eligible at the completed-day cutoff October 8 04:00 UTC. Original `captured_at` and `available_at` clocks are preserved, not backdated. Uncompleted market sessions cannot be selected, the query cutoff cannot exceed the recording clock, and an audit cannot be recorded before an input's physical capture. Missing adjustment references and scopes still block corroboration. SQL results and audits explicitly state `price_capture_pit_required=false`.

The SQL interface is:

```python
from decimal import Decimal
from backtest_data.prices.reconciliation_sql import reconcile_stored_supplier_prices
from backtest_data.prices.validation import ComparisonContract

result = reconcile_stored_supplier_prices(
    session, observation_ids,
    as_of=query_cutoff, recorded_at=recording_clock,
    contract=ComparisonContract("documented rounding evidence", tick_size=Decimal("0.01")),
    candidate_id=None, source_priority=("alpaca", "kis_overseas"), persist=True,
)
# The caller owns commit/rollback. persist=False performs an assessment only.
```

The adapter joins `PriceObservation`, `SourceContract` and `Security` by original IDs and verifies the stored basis against the registered API endpoint/flag. It reads `session_scope`, `adjustment_reference_basis`, `adjustment_reference_evidence_ref`, `upstream_id` and `processing_path` from explicit row evidence or contract specification. Existing supplier rows commonly lack the comparison scope/reference; those rows remain usable as declarations and incomparable for corroboration. Unsupported endpoints or contradictory stored basis are rejected.

Persistence appends an immutable `ValidationIssue` audit and, when a value is nominated, a `SelectionDecision` referencing that observation. Both writes share a savepoint. Repeating the same inputs, cutoff and comparison contract inserts zero new rows and preserves the original recording clock; audit/selection identity conflicts roll back together. No migration, acquisition request, current ticker remapping or production worker change is required by these modules. Query integration and live evidence acquisition are separate caller responsibilities.

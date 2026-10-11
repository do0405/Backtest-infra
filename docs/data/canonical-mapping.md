# Initial SQL canonical mapping and metric materialization

`fundamentals.mapping_catalog.default_catalog()` selects the immutable vocabulary for
`fundamental_mapping_v2`. `default_catalog('fundamental_mapping_v1')` preserves the
previous HTTP-only vocabulary and deployed hash. Its SHA-256 hash covers the exact concept entries,
namespace patterns, accepted units/scopes, mapping policy, and numeric policy.
Every canonical SQL row stores this hash, source signature, document hash,
parser version, source selection decision, and source processing clocks.
Changing semantics requires a new mapping version; reusing an existing version
with a different hash fails before writing any mappings.

The current catalog accepts pinned 2009–2026 `http://fasb.org/us-gaap/…` and
HTTP/HTTPS `xbrl.ifrs.org/taxonomy/…/ifrs-full` namespaces. Matching uses the exact
standard local concept and validates any retained prefix binding. Currency
amounts and currency-per-share measures are distinct. `USD/shares` becomes
`USD/share` without changing the amount or establishing a share basis.
Custom namespaces, dimensioned contexts, unsupported concepts/units/periods,
nil values, and invalid numerics stay in the source SQL tables.

The mapper preserves source scope and document accounting basis. An XML
dimensionless context does not establish consolidated scope. A duration does
not establish a fiscal quarter, year, fiscal lineage, or share basis. Tax-inclusive
revenue and depletion-bearing depreciation are deliberately excluded from the
initial standard revenue and depreciation/amortization vocabulary.

The [SEC API documentation](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
describes standard taxonomy concepts and units and separately describes the
entity-wide aggregation used by Company Facts. Those aggregation properties
are not inferred for a source filing XML context.

## Python integration

```python
from dataclasses import asdict
from backtest_data.fundamentals.materialize import materialize_canonical, materialize_metrics

mapping = materialize_canonical(engine, batch_size=500)
metrics = materialize_metrics(engine, as_of=cutoff, history_mode="reconstructed_public")
print(asdict(mapping))
print(asdict(metrics))
```

Both functions accept a PostgreSQL engine or an existing READ COMMITTED
SQLAlchemy session. Engine calls open their own READ COMMITTED transaction;
other existing-session isolation levels fail closed so a lock waiter cannot
reuse a stale snapshot and create duplicate canonical rows.
The default source scope is all persisted SQL rows; `issuer_ids` is an explicit
optional filter. Canonical mapping uses source-row locks and a mapping-version
advisory lock. Repeated work preserves the original canonical ID and clocks.
Revisions remain distinct source and canonical records.

Default runs freeze an input scan cutoff, defer inputs that arrive later, and
record each new mapping's clock after normalization. Explicit caller clocks
still fail if they precede an input. Materialization reads document metadata
without repeatedly loading the large source context inventory.

`canonical_to_financial_fact` validates explicitly stored period/basis metadata.
Qualified quarter/YTD/annual/TTM inputs need a recorded fiscal lineage, cycle,
year, applicable quarter/index, and nonempty qualification evidence references.
Unknown accounting/share basis becomes missing arithmetic qualification. The
adapter does not infer calendar or fiscal metadata from dates.

Metric materialization evaluates the registered pure financial calculator
catalog. Valuation definitions require separate price/share orchestration and
are excluded. Each persisted result pins its definition version/hash and registry
hash, numeric policy, UTC cutoff, history mode, actual selected canonical/source
IDs, and their selection decisions. The actual compatible document disclosure
profile is carried into the result. Unsupported profiles and unobserved/future
inputs are ineligible.

Availability is the maximum input public clock. Capture and materialization are
separate clocks. A later public-history calculation is allowed; an observed-system
calculation materialized after its cutoff is counted unavailable and not stored.
Default calculations check the actual completion clock against that cutoff,
including when evaluation crosses it after beginning with eligible inputs.
The result signature covers the selected SQL references, UTC cutoff, history
mode, complete registered definition, and numeric policy. Repeats reuse the
original result and processing clocks.

Summary `successful` counts actual non-null `ok` calculations, independently of
canonical mapping coverage. No-input results are counted unavailable and skipped
for persistence because there are no selected input references. Unknown duration
facts can therefore produce useful canonical mappings while successful metrics
remain zero. Accepted mapping/materialization decisions have `research_only`
evidence grade and do not certify production quality.

The initial orchestration requires qualified quarterly inputs for quarterly/TTM
metrics. It does not persist unregistered YTD-difference intermediate metrics or
infer fiscal qualifications. FCF growth needs explicitly qualified FCF input
facts; the registered FCF calculation itself uses the existing component gates.

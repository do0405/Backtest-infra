# Korean full-target backfill and KIS US price reads

The user authorizes collecting every Korean target and asks whether existing KIS credentials can also provide US prices, plus which additional APIs are needed. Keep the current full 17,758-entry denominator (KR4,507/US13,251), existing PIT/identity qualification boundaries, and immutable release history. No sample limits, trading calls, purchases, or account creation.

## Immediate execution

Resume current KR price and financial work in separate bounded runs, then arrange a resumable full-target drain that continues across batches while recording progress and source blocks. A budget limits one execution slice, never the target. Empty runnable work, provider access failure, and unresolved coverage are distinct states. Preserve API throttles; do not spin on quota/auth failures. Collectors should survive the current chat turn as a hidden local process when needed and expose file/SQL progress, with an explicit end condition and stop mechanism.

## KIS US contract

Official primary references:

- https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/overseas_stock/dailyprice/dailyprice.py
- https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/overseas_stock/dailyprice/chk_dailyprice.py

Endpoint `/uapi/overseas-price/v1/quotations/dailyprice`, TR `HHDFS76240000`, daily `GUBN=0`, explicit end-date `BYMD`, raw `MODP=0`. Do not reuse the domestic endpoint's raw option1. Preserve `xymd/open/high/low/clos/tvol/tamt` as Decimal values and the `rsym/zdiv` response identity evidence. Volume/turnover matching, feed scope, historical identity, corporate actions and adjusted-return qualification remain unverified.

Use only verified official KIS symbol/venue mappings to plan requests, including provider spellings for class securities. Do not guess NAS/NYS/AMS from an unrecognized target exchange or rewrite punctuation automatically. Keep unsupported/unmapped targets explicit in coverage. All KIS HTTP calls share the same account rate bucket and authentication instance where possible.

Append raw archives, price observations and capture-only timestamps through the existing audit conventions. Pagination must progress backward strictly, preserve requested start/end, reject future/out-of-range/duplicate or malformed pages, and distinguish source exhaustion from complete historical coverage. A valid current API response does not certify listing or delisting history.

## Ownership and review

Use one worker for KIS overseas request normalization/storage and tests, one read-only researcher for official master/API coverage and additional-source recommendations. Parent owns provider transport method, queue planning, supervisor/CLI integration, full-target background execution, actual probes, and documentation. Workers are not alone and must not revert other changes or recursively delegate.

Route: delegate; tier: inherited Sol; execution_mode: native. Independent primary-source investigation and isolated adapter tests improve correctness without overlapping writer scopes.

## Verification and reporting

Test raw-option separation, numeric/date/identity validation, idempotent capture replay, correction/reversion evidence, pagination gaps, market isolation and full-target planning against the real PostgreSQL test DB. Probe KIS US using current credentials, not a newly requested key. Report actual collected counts, queued/missing coverage and active process state. Recommend additional APIs by specific unmet data requirements; do not call a connected API proof of full coverage.

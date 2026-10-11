# Pragmatic daily PIT, provider price basis and remote pipeline

User authorization: use official source publication/filing metadata instead of requiring original archive comparisons; supplier raw/adjusted declarations are sufficient price-basis evidence and corporate actions are supplemental; remove obsolete migration cold copies after checking the active E disk; implement sustainable remote updates and local SQL delivery. Existing source rows/clocks, releases and collectors are preserved.

Architecture seams: metadata public-day evidence is an additive daily policy, provider price classification belongs to acquisition contracts, and remote delivery transfers immutable SQL evidence rather than changing its clocks. Strict v5-v7 releases remain reproducible. New daily queries choose accession-specific corrections only after their own public day and exclude future trading days. A source field's unknown value is not invented from an accounting period.

Route: delegate independent PIT and remote transport implementation to two existing native children (inherited parent model), no recursive delegation; parent owns price policy, release integration, production verification and explicit storage cleanup. This is direct implementation of the user's revised design, not a new approval gate.

- [x] Add daily disclosure metadata evidence and bounded SQL supplementation using actual SEC accession/filed and DART returned receipt/date metadata. Preserve first-seen/capture/original histories. Regression: original100 before correction versus80 afterward; unknown publication remains unavailable.
- [x] Add source-declared raw/adjusted/split/dividend scope policy and relax corporate-action completeness gating for the new research policy. Keep volume basis separate, completed-market-day cutoff and old strict policies.
- [x] Connect daily policy in query/materialization/release CLI; execute actual SQL supplementation and query verification.
- [x] Inspect Docker settings, active disk/volume health and exact migration backup inventory. Delete only redundant cold VHD copies within E:\DockerData\storage-migration-20261009; preserve active E data, existing SQL and operation evidence. No compact/reset/restart of running Docker.
- [x] Implement compressed versioned delta batches, manifest/hashes/parent references, original IDs/timestamps, import receipt and replay safety. Demonstrate SQL round-trip and rollback on tamper/missing parent.
- [x] Add bounded GitHub Actions/cloud-compatible acquisition entrypoint and persistent checkpoint/artifact transport; daily/rolling refresh planning and provider ownership guards. Existing local ownership stays enabled until remote configuration is complete. No automatic public repository conversion or paid cloud deployment.
- [x] Run relevant unit/PostgreSQL tests, read actual SQL results, document deployment configuration still missing and update the existing two-hour monitor to the revised policies.

Migration allocation: c71302d6b909 daily metadata, c71302d6b910 remote import receipts, c71302d6b911 price classification if needed. Parent registers models and coordinates additive production upgrades. No commits/pushes or cloud settings are implied by local implementation.

## Execution verification and remaining activation boundary

Production schema was upgraded additively through c71302d6b912. Full-target metadata supplementation and US supplier split-price traversal run independently; existing price/fundamentals/KRX/supplemental workers were not stopped or restarted. Fixed metadata audit: 7,057 documents, original 17,758 securities. Audit completion is distinct from full data coverage.

Actual supplier API rows committed with the SQL page cursor, and production publication-bound checks preserved original source clocks. A five-row actual-source delta plus parent closure was exported, imported and replayed; local sync CLI also confirmed replay zero. Twelve targeted PostgreSQL tests plus six remote SQL tests have passed. Related unit tests and fixed fixture reruns are recorded in the execution logs; overlapping suites are not summed.

Two redundant cold VHD copies were deleted only after checking active E storage, SQL health, inventory and exclusive unused handles: 89,634,373,632 bytes (about83.5GiB). Active E disk and operation evidence remain. No active compaction or Docker reset/restart was performed.

Remote code includes explicit ownership, current refresh windows, portable partial-page/child work, exact JSONB/Decimal clocks, contiguous chunks and private local import. Current GitHub workflow is inactive. The user has not selected a repository/GCloud project/private store. Catalog-only bootstrap and flags are not a verified identity handoff: existing natural-key/UUID compatibility and operational source transfer still require an actual activation procedure. No monthly free-cost or production-cloud-readiness claim is made.

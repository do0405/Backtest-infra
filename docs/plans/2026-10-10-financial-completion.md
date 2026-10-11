# Financial context completion and full SQL audit

The completion worker covers all stored financial documents in small transactions. Source capture does not qualify historical publication. Separate publication certificates and the strict release query gate remain necessary.

## Findings grounded in stored evidence

The old numeric XBRL parser discarded nonnumeric SEC DEI report metadata. The canonical mapper deliberately left fiscal lineage, share basis and unknown statement scope unqualified. These independent gaps explain why a large source inventory can produce no TTM, ratios or growth.

A bounded production validation of SEC document `001b5427-f06e-422f-b325-08f0e8d12ce4` processed all 833 stored source facts and committed 36 additional standard mappings with US GAAP vocabulary evidence. It qualified no fiscal periods. SQL audit recorded 36 missing fiscal metadata qualifications, 36 unknown scopes, eight missing share bases, 534 unsupported dimensional accounts, 263 unsupported taxonomy/concept accounts, and one unqualified historical publication status. This validated the full worker entry without limiting its eventual population.

The inspected DART JSON document `0009db95-1497-42e1-a471-592f0cc715cd` had business year, report code and amount labels in its stored `source_row`, but no `*_dt` date fields. These labels cannot supply annual dates, fiscal quarter ordinals or publication certification. JSON account metadata dimensions and provider vocabulary also remain outside the exact XBRL mapping catalog.

## Supported completion

- `completion.parse_sec_xbrl(content, form=...)` preserves six bounded DEI fields and their exact original SHA-256 once per document, on its first numeric source record. Metadata is charged to the existing emitted-evidence budget. Optional metadata overflow retains all validated numeric facts and records an unavailable marker, or `financial_extraction_v5_sec_metadata_budget_unavailable_v1` when even a small marker exceeds the budget. Numeric/context counts remain exact.
- Exact Q1 and full annual contexts can acquire fiscal lineage when focus, entity, hash and stored issuer identity agree. Short transition periods remain unqualified. Q2/Q3 focus does not establish fiscal year start or YTD duration.
- Actual instant balances at qualifying closing/opening boundaries retain their original dates and can acquire fiscal lineage. Instant year/quarter ordinals are not invented.
- Recognized standard US GAAP and IFRS namespaces establish accounting vocabulary when unknown. They do not establish consolidated scope, common stock scope or comparable share basis.
- `completion.complete_quarter_inputs(...)` adds compatible, visible adjacent YTD differences for non-EPS inputs after public revision selection. Original canonical dependency IDs remain complete. Reported quarters take precedence. EPS is never differenced.

The [SEC XBRL guide](https://www.sec.gov/files/edgar/filer-information/specifications/xbrl-guide-2026-06-29.pdf), sections 3.1.2 and 3.1.8, distinguishes report focus and required context end from every numeric duration's fiscal start. The [FASB taxonomy FAQ](https://www.fasb.org/taxonomyfaq) describes US GAAP vocabulary and basic/diluted EPS concepts; those concepts do not prove comparable shares across reporting versions.

## APIs

`supplement_sec_metadata(session, document_id=..., content=..., captured_at=...)` accepts an in-memory refetch of the existing SEC URL only when its SHA-256 matches the stored version. Changed content is rejected. It writes a `ValidationIssue` assertion under `sec_fiscal_metadata_v1`. Source/document/filing clocks remain unchanged; the assertion has separate capture/recording clocks. Six DEI fields are retained, without the filing body.

`materialize_financial_completion(engine_or_session, document_ids=[...], max_documents=1, max_source_facts=50000)` creates append-only canonical mappings under `financial_semantic_completion_v1`, preserving physical availability and hash-bound qualifications. Its document audit stores all missing reasons.

`original_metadata_qualified(basis, evidence_rows, resolution)` is the additional v7 fiscal metadata publication gate. Metadata-dependent inputs require an exact selected original body hash equal to `basis.source_document_hash`. Numeric equivalence across different bodies cannot certify captured nonnumeric fiscal labels. A failure is `semantic_public_version_unqualified`; it never falls back to capture as certified history. No future metadata digest extension is assumed.

Audit schema `financial_input_gap_audit_v1` contains an explicit gap entry for every registered metric, including revenue/EPS/operating-income QoQ, YoY, TTM YoY and annual growth IDs. The six valuation IDs (PER, PBR, market cap and EV families) explicitly retain `price_share_class_lineage_unavailable`; price, common share class and adjustment inputs are not guessed. These entries describe this document's input gate. Issuer evaluation remains separate.

## Full population worker

Run `.\.venv\Scripts\python.exe -m backtest_data.fundamentals.completion_worker --checkpoint artifacts/clean-completion/financial-progress.json --max-source-facts 50000`.

The default page is one document. No sample count or batch-count limit applies unless `--max-batches` is supplied for validation. A dedicated PostgreSQL advisory lock excludes duplicate workers. SQL failures emit redacted `blocked` receipts with the recovery wrapper's `database_error` classification and exit code 2.

Pending membership uses current SQL audit coverage and metadata recording time. The UUID keyset wraps before completion, including newly collected IDs below the cursor. Restarting a completed checkpoint checks for new documents. An oversized source inventory is deferred entirely: no partial canonical materialization, SQL `completion_source_fact_limit`, and the exact deferred ID in the checkpoint. Retry those explicit IDs with a larger verified budget. `complete_with_deferred_documents` describes completed audit traversal with outstanding full inventories.

The core worker downloads no sources. Run the optional full SEC source stage first with `.\.venv\Scripts\python.exe -m backtest_data.fundamentals.completion_metadata --checkpoint artifacts/clean-completion/financial-metadata-progress.json`. It processes all pending complete SEC documents one at a time through the existing shared SEC/ProviderHTTP budget, before document write locks. The first auth/access or retryable source block records its distinct source reason and stops the whole stage. Restarting that blocked checkpoint stays quiet unless `--retry-source-block` is supplied. A missing individual document or oversized response is a document-level gap. SQL errors use the same redacted recovery receipt as the core worker. Source bodies are never written to disk.

The actual one-document SEC probe succeeded with exact hash identity and metadata assertion `d5648755-f43c-45f9-9b50-affb2b3a5492`. Its metadata was reported, but 13 mapped facts still needed independent fiscal-year-start evidence and 23 were outside the report focus. It added 36 context versions before semantic state reuse was tightened; future changes to missing reasons do not duplicate the same semantic canonical state. All 36 inputs retained unknown scope, eight lacked share basis, and historical publication stayed unqualified. Existing collector processes need a restart to load the new parser hook.

## Verification and operational dependencies

The latest unit-only verification passed 22 tests, including original-body versus numeric-only semantic publication proofs, numeric preservation on optional DEI overflow and all six valuation gap entries. Nine targeted PostgreSQL cases passed before the production server recovery hold, covering supplements, preserved clocks, new metadata revisiting old audits, worker exclusion, whole-inventory deferral and bounded metadata source failure. The larger-budget deferred resume regression also passed before the hold. The parent coordinates the combined SQL rerun after recovery.

Audits retain their original fact budget. Increasing `max_source_facts` revisits previously deferred documents; a resolved document is removed from the checkpoint's deferred list. Unchanged or lower budgets reuse the durable deferral and do not loop indefinitely.

The issuer index bounds existing canonical lookup. Full launch is coordinated with the parent runtime wrapper and annotation indexes: without document/rule annotation indexing, even a bounded operation can incur a costly lookup. No schema or migration head is added here.

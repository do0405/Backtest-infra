# KRX Publication Cost Implementation Plan

> **For agentic workers:** Use inline execution with a read-only spec/plan reviewer. Preserve all existing collection processes and unrelated workspace changes.

**Goal:** Remove repeated manifest and row validation from hosted checkpoint publication without weakening privacy, integrity or stream atomicity.

**Architecture:** Codec owns typed decoding and privacy checks. Bundle owns exact-byte manifest and row integrity. Folder transport owns complete group preflight, verified staging and atomic head advancement. The hosted script consumes verified counts without an additional full scan.

**Tech Stack:** Python 3.13, pytest, existing PostgreSQL test database, GitHub Git API.

- [ ] Add failing tests for single-pass manifest decoding, bounded field classification, late private URL rejection, one source plus one copy row scan, complete group counts and failure preserving the previous head.
- [ ] In `codec.py`, cache field-name classification with a finite bound and validate decoded URL strings in `unpack`; remove the redundant whole-tree `pack` after decoding.
- [ ] In `bundle.py`, parse JSON once and validate that parsed value before returning the original packed representation. Keep exact manifest hash and all file/expansion bounds.
- [ ] In `folder.py`, separate validated staging from source validation, bind it to the original manifest hash, fully validate copies, collect table counts in preflight, and expose counts only after head advancement.
- [ ] In `worker.py` and `remote_release_cycle.py`, carry successful group counts to the commit and remove the post-archive full data scan. Add bounded, safe payload size/count phase diagnostics.
- [ ] Run focused regressions, existing remote bundle/folder/codec/quota/execution tests and separate PostgreSQL export/import tests. Benchmark the same immutable real checkpoint plus representative offline rows.
- [ ] Obtain independent read-only correctness review; address actionable findings.
- [ ] Build and test an exact overlay on fresh remote main, verifying source hashes. Publish only those reviewed files with a non-force ref update and read back the tree. Preserve schedules, limits and all current runs.
- [ ] Save evidence, update the existing monitor with the new pending natural-run SHA, and report deployed code separately from future Release/SQL completion.

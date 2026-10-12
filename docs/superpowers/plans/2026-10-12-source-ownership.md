# Source ownership implementation plan

> Execute inline using superpowers:executing-plans; obtain one independent plan and final patch review.

**Goal:** Keep local unfinished historical work and independent products running while admitting forward raw-price and filing refresh only in the existing hosted lane.

**Architecture:** Add a strictly bound opt-in planning fence to the local manager. Clip newly constructed hosted correction windows at the original history end; preserve all existing work. Deploy through a new hash-pinned runtime and a manager-only handoff.

**Tech stack:** Python, pytest, Windows singleton / process receipts, GitHub immutable Releases and Git refs, existing PostgreSQL importer.

## 1. Regression evidence

- [ ] Add tests/unit/test_source_ownership.py for policy default / binding / source filtering / frozen planning and manager no-launch behavior.
- [ ] Add tests/unit/test_offline_ownership.py for project gates, future correction boundaries and legacy cycle / gap preservation.
- [ ] Run these tests and save failing evidence in artifacts/source-ownership-20261012.

## 2. Small implementation

- [ ] Modify ops/source_cadence.py: validate ownership file and choose only remaining local refresh sources; retain standalone defaults and frozen planning source binding.
- [ ] Modify ops/continuation_host.py: read artifacts/source-ownership.json while holding the original domain lock; admit existing pending/retry work first using the small queue check, then return remote_owned without new SQL planning when all role refresh sources are hosted. Unknown queue state waits safely.
- [ ] Modify ops/continuation_manager.py: treat remote_owned as waiting_for_remote_refresh and never launch for this state; pending original jobs continue normally.
- [ ] Modify offline/acquisition.py: use common publication gates, bind forward_start to catalog.output_end + one day for future plans only, and bound new recent correction cycles. Preserve existing cycles and gap retries verbatim.
- [ ] Run focused local manager/cadence and offline contract tests.

## 3. Exact runtime / independent review

- [ ] Extract the existing v4 runtime into a separate candidate; overlay only acquisition and common cadence files. Compare every other packaged file to the v4 archive.
- [ ] Run exact candidate offline unit and isolated _test SQL importer regressions; no live source API requests.
- [ ] Review immutable candidate hashes, policy tests, rollout checks and source-product matrix.

## 4. Deploy and observe

- [ ] Publish new v5 runtime as a fresh immutable Release and verify downloaded SHA-256; do not activate it yet.
- [ ] Publish reviewed scoped source files/docs to remote main without force, preserving concurrent data commits; no dispatch/cancel.
- [ ] Check existing receiver identity and saved SQL receipts. Save bound policy activation evidence and atomically write opt-in file.
- [ ] Freeze and replace only the manager controller, with current identity / no launch_uncertain / lock release / WMI intent and receipt checks. Reuse final old role state, record preservation and natural source generation transitions.
- [ ] After the local fence is applied by the new manager, switch only runtime_tag/runtime_sha256 in existing OFFLINE_CORE_CONFIGURATION. Preserve all other config fields and secrets. Current hosted runs retain their loaded v4 and existing cycles remain legacy work.
- [ ] Update quiet heartbeat handoff with new code/runtime hashes, ownership, existing receipts and pending natural-run limitations. Report concrete matrix and observed progress; do not claim unobserved release / SQL completion.

# Live remote refresh and local automatic import

User authorization: activate GitHub Actions in do0405/Backtest-infra, transfer
checkpoints/ownership without duplicate local acquisition, preserve existing
data, verify actual provider execution and production SQL import. Public source
and data/results were authorized; credentials remain private Secrets.

Architecture seam: source-account ownership stays separate from transport and
local SQL import. KRX is the first independent account. Current local manager's
operator-stop receipt fences automatic relaunch before exact native shutdown.
All original jobs and committed checkpoints remain untouched locally. A frozen
portable snapshot restores only into an empty remote queue, with original IDs.
Every original raw hash/UUID is carried as metadata to prevent cross-host natural
key collisions, including identical empty KRX responses. Original full catalog,
target count17758 and original date range remain unchanged.

Transport: authorized public GitHub Release assets hold the constant original
catalog bootstrap, compact portable checkpoints and independent, complete FK/raw
closure deltas. Draft release upload precedes publication; checkpoint advances
only after all declared hash-verified assets are published. Releases are selected
by stream/generation/sequence, not unrelated repository artifacts. No old source
data is deleted, no old capture/publication clock is rewritten. Each fresh runner
restores catalog plus latest compact checkpoint; past numeric history stays in
local SQL and immutable delta releases, avoiding cumulative replay and90-day
artifact expiry. Bounds remain per asset/run and disk rather than total history.

Local sync: one detached singleton with a persistent lock polls committed release
receipts, downloads bounded fresh directories, imports source tables and receipt
atomically in sequence, records exact remote/local times, retries temporary DB/
network failures, and records permanent problems without exposing credentials.
On restart SQL receipts allow zero-insert replay. The local importer never claims
or resets source jobs, leases or checkpoints.

Steps:
- [x] Test bounded bulk FK export and exact raw identity restore.
- [x] Implement Release checkpoint/delta transport, trusted scheduled workflow,
      singleton sync and source handoff gate. Review source/secret publication.
- [x] Freeze original KRX state after identity-bound quiesce and manager fence;
      preserve all other running workers and online index builds.
- [x] Publish bootstrap and ownership Secret; execute short real remote run,
      verify original scope/checkpoints and new official response evidence.
- [x] Verify actual automatic local SQL rows/capture clocks and zero-insert
      committed replay. The first bundle contains4,972KRX observations and
      5,014 total new rows. Hosted scheduling and singleton local polling are
      enabled; later bundles continue automatically.
- [ ] Finish the separate full-generation verification of both published runs;
      do not report24,047cloud observations as all committed locally.
- [ ] Expand explicitly fenced account groups only when identity/task closure
      is verified; record each connected source and external access gaps.
- [x] Finish approved research report/export improvements and actual browser
      checks; do not count engine tests or partial source samples as full clean.

No extra design approval is needed: this executes the user's approved remote PRD
and latest explicit activation request. Direct parent owns remote/operation files;
two bounded native agents own raw identity integration and research followup.

Verified 2026-10-11: original 39,330 KRX jobs / 114,296 raw identities,
22 catalog chunks, hash-verified bootstrap Release, fenced local KRX owner,
registered ownership Secret, two-hour hosted schedule and a singleton login
sync task. First hosted attempt found a missing pypdf runtime dependency before
acquisition; the fixed run 38115965869 completed and published 10,074 KRX
observations and 28 captures. Run 38116683206 verifies continuation from its
checkpoint. Production SQL import is being verified by the actual automatic
importer, including a tested Windows download path-length correction. The first
committed bundle and exact original samples/replay were verified in production
at07:43:36UTC; full-generation imports remain in progress. Other
provider accounts remain locally owned; this is a staged source handoff.

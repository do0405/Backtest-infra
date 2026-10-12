# KRX publication cost repair

The user authorized a root cause repair after patched natural run 38196650268 timed out, then explicitly authorized measured execution-limit adjustment together with time and volume bounds. Preserve the 6000-second acquisition ceiling, its original absolute deadline, 1200-second export reservation, existing quota charges, ownership and live local processes.

## Evidence

Acquisition ended with 1199.78 seconds remaining. Checkpoint export took 507.95 seconds; folder publication took 601.30 seconds. Archive preparation left 60.39 seconds. No release upload start or new committed Release was observed. The receiver remains at sequence 11, confirmed by five small SQL receipt reads.

An already received 54,576,126-byte manifest has 39,470 portable tasks and the original raw identity index. A read-only profile found 2,260,426 repeated private-field name checks in one manifest read. The manifest currently parses JSON twice; codec decoding then rebuilds the whole decoded tree for privacy checks. Group publication scans source payloads during preflight and again during each publish, then verifies the copy. Release counting repeats row validation after archive preparation.

## Selected design

Retain the v1 bundle and Release formats and full fail-closed validation. Decode typed values and validate decoded URL/privacy rules in one pass; cache only classification of bounded field names. Parse the manifest JSON once, validate that same parsed tree, and retain its existing packed checkpoint representation.

Group publication validates every source row before staging. An internal staging function reuses only the locally validated manifest and its exact hash, verifies the staged copy fully, and advances the head only after all bundles succeed. Count tables during this complete source pass and return those counts to the hosted worker. The release script consumes them before archiving and does not rescan data afterward. Ordinary publish and receiver APIs retain their validation and replay behavior.

The same real checkpoint profile decreased from 11.40 to 2.32 seconds locally after removing the repeated work. This is not a remote end-to-end timing claim. At the user's later explicit request, the workflow hard limit is the original two-hour completion target plus two hours of grace (240 minutes), while leaving the immutable two-hour quota expiry and 100-minute absolute acquisition deadline unchanged. Hosted acquisition stops before the next claim at 256 HTTP attempts including retries, 500,000 inserted observations or 128 MiB of committed raw responses. HTTP admission checks both before reserving and immediately before each attempt; reservations remain charged even if their wait outlives the invocation. One already begun response is committed whole and can overshoot the row/byte soft bounds. The provider already limits a response to 32 MiB.

The three earlier successful validation batches lasted 610, 637 and 699 seconds; they used a small acquisition window and do not establish the typical duration of the larger batch. Its typical new completion time remains pending real natural-run observations. The two-hour completion target is an explicit initial envelope, not a measured average or ETA. Upload may continue within the outer four-hour guard instead of being cancelled by a separate five-minute subprocess timer.

Healthy source work remains a continuous checkpoint pipeline across naturally scheduled generations. GitHub-hosted jobs have an execution limit; removing a job timeout does not create an unlimited persistent process. No source ownership moves or self-hosted service is introduced here.

New allowance reservation uses the lesser of the original time-derived allowance and the 256-request envelope (264 pessimistic credits including the original eight-credit margin). Earlier charges are not refunded, and the 9500/day limit and 10-second interval remain unchanged. Local KRX APIs retain their existing behavior when optional hosted limits are absent.

Increasing only job time or introducing a new segmented checkpoint format would leave the repeated-work problem or change receiver compatibility. The selected change removes that cost and jointly bounds acquisition and publication using the existing Release format.

## Invariants and acceptance

- Preserve every original row ID, Decimal, clock, typed pagination cursor, raw hash, task, attempt, provider rate state and quota charge.
- Reject private keys and URLs at arbitrary depth, malformed typed values, nonfinite values, row expansion/count violations and altered manifests or payloads.
- Source and copy both receive complete row validation; group failure leaves the previous head unchanged; replay remains idempotent.
- Counts describe all validated group rows and become available only after successful group publication.
- Offline benchmarks must use the same real received manifest and representative multi-bundle rows, and report local timings as local evidence only.
- Test the exact remote candidate and publish a non-force overlay against a freshly checked main. Do not dispatch, cancel, restart collectors or import anything to production. Actual Release and SQL success require the next natural run and existing receiver receipts.

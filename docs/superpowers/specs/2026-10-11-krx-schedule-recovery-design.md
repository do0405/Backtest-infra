# KRX scheduled execution recovery

The user has authorized restoring continuous remote KRX collection and verifying delivery to local SQL, without changing existing data, checkpoints, or ownership.

Observed failure: the active default-branch workflow contains a valid two-hour UTC schedule, all four admission flags are enabled, and manual execution and local receipt import succeeded, but scheduled runs were never created. GitHub's internal scheduler reason is not exposed by the available API. Re-register the schedule and provide an independent local watchdog for missed invocations; do not claim a confirmed GitHub incident.

The watchdog owns invocation only. It must not collect provider data, access provider secrets, import SQL, alter job leases, or transfer ownership. The existing remote workflow remains the sole KRX collector; the existing local receiver remains the sole importer. Keep the workflow's existing concurrency group and cancel-in-progress=false, published checkpoint lineage, account quota, and original handoff unchanged.

Run the watchdog in a separate windowless AtLogOn task, with an OS singleton lock and IgnoreNew. Poll the official GitHub CLI using its existing configuration directory. Admit a continuation only when ownership/handoff are verified, all remote flags remain enabled, the workflow is active, no run is queued/running/waiting, and the last run is older than two hours plus fifteen minutes. An operator-disabled pipeline stays disabled. Unknown workflow states and unclassified failures fail closed.

Persist a UUID invocation intent before dispatch. Put that UUID in the workflow run name using an optional input. Reconcile the run by UUID before any further dispatch. An ambiguous request outcome never causes a blind retry. Authentication or permanent remote failures require attention; transient read failures retry without a total retry or runtime limit. This watchdog needs the local computer running; GitHub's native schedule remains the primary remote trigger when the computer is off.

Use one 300-second proof batch, then normal 6000-second batches. Verify the new published Release links to the prior sequence/hash and unchanged handoff/catalog, then wait for the existing receiver's SQL receipts and bounded original-row comparisons. No collector or importer replacement is needed.

Regression evidence must cover active/pending suppression, disabled flags, cadence/grace, persisted-before-write intent, accepted-but-lost response, process restart reconciliation, unknown outcomes, permanent failures, and unchanged concurrency. Production evidence must include a watchdog-origin run, new valid API observations, published lineage, new committed local SQL receipt, and unchanged ownership.

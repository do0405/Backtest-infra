# Continuous checkpoint collection

User authorization: renew execution budgets and recover transient failures until planned work is exhausted, without stopping current workers, duplicating suppliers, changing data/checkpoints, or notifying about ordinary recovery waits. The existing heartbeat stays read-only and reports only required operator action.

Architecture: a single detached continuation manager adopts the eleven existing wrapper/native trees. It never starts a replacement while a verified wrapper or child is alive. Replacements reuse original role locks, supplier lanes, manifest/audit/progress paths and SQL fencing. Each child keeps its bounded execution budget; the manager has no overall budget or retry-count cap. Cooldowns prevent busy retries. Unknown/schema/auth/identity/storage errors remain explicit action states. Request traversal completion is separate from clean completeness.

- [x] Test policy decisions: budget rollover beyond six attempts, transient DB/lease/network/quota/maintenance, mixed auth blocks, explicit completion, unknown failure, disk guard, source queue exhaustion and dependency waits.
- [x] Test controller adoption, child-orphan wait, singleton/domain locks, immutable generation receipts, atomic metadata updates and failed/unknown WMI launch outcomes without duplicate starts.
- [x] Implement the pure continuation policy and a small Windows host adapter using read-only CIM identity checks and detached WMI launch. Do not alter existing live workers.
- [x] Preserve clean pipeline successful stages when a new generation resumes; inspect nested failure receipts to distinguish transient stage failure from permanent error.
- [x] Launch one manager after tests, verify unchanged current worker identities and new SQL commits, then enable the updated quiet heartbeat. Keep remote deployment inactive.

Ownership: continuation layer owns lifecycle/cooldowns/metadata; collectors own API budgets, transactions, cursor advancement, owner/fencing/TTL and source validation. No lease rewrites, requeue of unsupported records, new audit, source-policy relaxation, destructive Docker action or credentials in logs.

Validation: deterministic policy/controller tests plus real Windows adoption, duplicate-lock prevention, a separate harmless budget-rollover fixture and fresh production SQL/process checks. No production collector is killed for testing.

Evidence: artifacts/continuous/production-verification.json, budget-rollover-verification.json and unit-verification.txt. The original eleven worker PIDs remained unchanged; one WMI venv launcher/native manager holds the singleton lock. Real isolated WMI children rolled over three budgets with unique PIDs and unchanged production metadata. The helper tracks original supplier configuration file metadata without logging secrets; authentication/permission needs are quiet while waiting and may retry after a file change or a slow access recheck. Successful finite source/audit passes stop renewing without claiming clean coverage. The user account's two-hour heartbeat remains ACTIVE and only notifies about required action.

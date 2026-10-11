# Background checkpoint continuation

The user requested that collection continue in the background after repeated
lease-loss partial exits. Windows WMI wrappers already detach from the app.
The observed failure is SQL work outlasting a fenced five-minute lease, not
the desktop app ending an execution session.

## Evidence and change

- KIS US and Alpaca raw price stores read every historical observation for the
  security before writing a small page, including empty pages. A PostgreSQL
  regression reproduces loading unrelated 2016 observations for an empty page.
- Normalize the page first, then retrieve prior capture/replay evidence only
  for accepted sessions. Keep immutable source rows, capture timestamps, source
  contracts, signatures and prior-per-session revision linkage unchanged.
- KIS US previously made one price INSERT and one decision INSERT per bar.
  Write up to 100 prices/decisions per statement, fence after each bounded
  write, and keep data plus checkpoint in the original atomic page transaction.
- KRX still used a pre-selection clock. Use the already implemented post-lock
  acquisition clock; keep the five-minute TTL, ownership and fencing checks.
- Add opt-in detached continuation after explicit lease-loss-only partial
  receipts. Reuse the original command/source group and SQL checkpoints.
  Hold the domain file lock during both the child and the cooldown.
- Retry delays: 60, 120, 300, 600, 1200, 1800 seconds, at most six lease retries.
  DB transport recovery retains its separate six retries. One deadline includes
  all attempts and waits. Provider/unknown errors, mixed blocks and budget ends
  do not restart. Exhaustion remains incomplete and is reported.

The recovery layer owns waiting and receipts; the collector alone owns job
acquisition and page transactions. Recovery never mutates SQL leases, cursors,
source data or supplier permissions. Existing workers keep their old policy
unless launched with the new opt-in wrapper; live workers are not interrupted.

## Operation and verification

Start only metadata-confirmed terminal workers whose native processes and
domain locks are absent/released, with no live SQL leases. Preserve previous
metadata and monitor state, then register each new WMI wrapper immediately.
Keep active fundamentals, supplemental, adjusted-price, VWAP and PIT workers.

Verify real PostgreSQL revision/replay/rollback regressions, slow KRX acquisition,
finite mixed-error/budget policy, domain-lock continuity and separate receipts.
After launch verify native process identity and actual attempts/checkpoint commits.
Startup alone is not dataset completion or proof every page meets its lease.

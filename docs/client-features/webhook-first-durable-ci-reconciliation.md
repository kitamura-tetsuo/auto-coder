# Webhook-first durable CI reconciliation

If another CI delivery arrives while its SHA-to-PR lookup is running, the older
lookup result is discarded and the newer durable correlation retains its quiet
window for retry. This expected supersession does not terminate the invalidation
controller, apply stale PR membership, or prevent other ready PR batches from
being promoted. The pending correlation survives restart.
If a companion controller does fail, supervisor cancellation waits for owned
local work to finish and then propagates, preventing cancelled workers from
continuing or leaving daemon shutdown waiting forever. Explicit graceful drains
still finalize the owned operation's result through their existing checkpoints.

Manual workflow dispatch now publishes a durable repository, pull request,
exact-head and workflow watch before making the external request.  CI webhook
bursts advance that obligation through the shared two-second quiet window,
while a retained 300-second deadline provides targeted missed-event recovery
across restart.  The shared invalidation controller promotes due watches into
the normal pull-request evaluation path; no per-PR polling thread, run-appearance
timeout, or monitor-owned merge path remains.  Dispatch claims remain a separate
suppressing safety record when observations are missing or unavailable.

An ordinary durable PR reevaluation retires every stored CI watch for that PR
before completing its generation when its cache-bypassing GitHub read confirms
the PR is closed, merged, or authoritatively absent. Operational read failures
retain both the watch and reevaluation responsibility. Open and reopened PRs
continue to retire only obsolete heads and activate the current-head watch, so
periodic recovery resumes only after a fresh authoritative open observation.
These terminal/open lifecycle transitions precede author-admission filtering;
changing an allowlist cannot strand or revive watches contrary to strict state.

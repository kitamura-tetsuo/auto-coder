# Graceful daemon shutdown

The long-running `process-issues` daemon handles both SIGINT and SIGTERM by
entering an observable draining state. Draining stops candidate admission and
capacity/provider dispatch, preserves queued and newly delivered webhook
invalidations for restart, and waits *only* for an already-admitted, still
in-flight LLM invocation to reach its durable checkpoint (Issue #2010; see
[LLM invocation admission and shutdown protection](llm-invocation-admission.md)
for what that boundary is). Durable remote provider work does not delay
shutdown either: a local handoff receipt settles the wait without waiting for
the remote task itself to finish.

Every other local operation -- an update check, a session-reconciliation
scan, a worker's post-invocation git/GitHub bookkeeping, a queued or
not-yet-admitted validation -- is interrupted instead of awaited: its
`new_work_allowed()`-guarded business logic bails out at its next check
point, and any subprocess it owns (see `shutdown_interrupt.py`) is killed
immediately, unless that subprocess belongs to the one still-admitted
invocation's own controlled provider call or tool tree. None of this
unrelated work's own natural completion, external response, maintenance
interval, retry/backoff deadline, or normal network/process timeout ever
delays the daemon's exit; a stopped mid-flight operation leaves its work
durably pending for a fresh authoritative reevaluation on restart rather than
being executed merely to empty a queue. A second SIGINT explicitly
force-stops the drain and is logged separately as forced, not graceful.
Repository Compose services allow a 30-minute stop grace period, and the
image's exec-form entrypoint delivers Docker's SIGTERM directly to
Auto-Coder.

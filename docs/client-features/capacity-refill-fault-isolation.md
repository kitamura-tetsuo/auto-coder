# Capacity-refill fault isolation

The long-running automation engine contains ordinary Python exceptions at the
capacity-refill boundary. A local implementation-slot observation failure is
reported as `capacity_unavailable`; the engine retains its refill opportunity
and retries only that observation after a bounded delay. A validated read of
the real slot store clears this transient disposition and immediately permits a
fresh refill evaluation.

Every read-only capacity check in a refill pass uses the same validated slot
snapshot boundary. A failed entry, per-candidate, or post-dispatch observation
therefore remains `capacity_unavailable`; post-dispatch failures also keep the
refill opportunity pending when the recovered store identity is unchanged.

An unexpected candidate failure is not safe replay evidence. The engine records
an `intervention_required` pause for the actual Issue (or the repository when a
target cannot be identified). A pending-work persistence failure always widens
that pause to the repository because no Issue in the repository can prove a
durable timed handoff while the shared store is unavailable. Existing records
are left intact. Other target-scoped faults leave established invocation
ownership intact and continue considering independent Issues. All automatic candidate origins
consult the same engine-lifetime pause before beginning evaluation, so a queued,
pending-work, or newly enumerated copy cannot bypass it. Intervention pauses do
not expire and are discarded only with the engine instance; they are not a new
durable replay authority.

Supported strict-snapshot admission refusals encountered during refill
enumeration are retained as ordinary timed pending work before another target
read can occur. Pending-work entries consult intervention barriers before any
strict refresh or revision supersession, leaving their durable effects
unfinished while paused. An unreadable pending-work store instead creates a
repository intervention fault with ERROR diagnostics.

`get_status()` exposes active entries through the additive `refill_faults`
collection. Each entry includes repository, optional target, phase, exception
class, disposition, and a UTC epoch `retry_not_before` (or `null`). The first
observation of a changed fault is also logged at ERROR with its traceback.
Cancellation and process stop signals are never converted into refill faults.

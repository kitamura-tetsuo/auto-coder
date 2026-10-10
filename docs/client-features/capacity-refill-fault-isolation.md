# Capacity-refill fault isolation

The long-running automation engine contains ordinary Python exceptions at the
capacity-refill boundary. A local implementation-slot observation failure is
reported as `capacity_unavailable`; the engine retains its refill opportunity
and retries only that observation after a bounded delay. A validated read of
the real slot store clears this transient disposition and immediately permits a
fresh refill evaluation.

An unexpected candidate failure is not safe replay evidence. The engine records
an `intervention_required` pause for the actual Issue (or the repository when a
target cannot be identified), leaves established invocation ownership intact,
and continues considering independent Issues. All automatic candidate origins
consult the same engine-lifetime pause before beginning evaluation, so a queued,
pending-work, or newly enumerated copy cannot bypass it. Intervention pauses do
not expire and are discarded only with the engine instance; they are not a new
durable replay authority.

`get_status()` exposes active entries through the additive `refill_faults`
collection. Each entry includes repository, optional target, phase, exception
class, disposition, and a UTC epoch `retry_not_before` (or `null`). The first
observation of a changed fault is also logged at ERROR with its traceback.
Cancellation and process stop signals are never converted into refill faults.

# Production ownership binding for durable Implementation generations

The durable owned-start tombstone described above is now bound to the real
production implementation-ownership authority (the logical implementation-owner
store's owner records, retained local executions, provider sessions, and
implementation-PR membership) at the actual admission boundary used today,
ahead of the dedicated Implementation worker lane. Production ownership for an
exact Implementation generation is acquired the moment a durable local
execution is admitted for it; an unpersisted local call, a bare idle owner
reservation, a slot-availability check, or a specification-review decision
never marks a generation owned on their own. Once acquired, the captured
generation survives finishing or reclaiming a stale local execution, a
provider session ending, or an owner otherwise becoming releasable, and is
recovered from durable evidence rather than fabricated if a crash interrupts
the handoff before the tombstone itself is persisted. A different,
superseding generation (from a specification, membership, or reparenting
edit) is always a distinct admissible attempt: the superseded generation's
tombstone is preserved rather than rebound, so an exact reversion to it stays
suppressed even though the owner record itself can only track one current
generation at a time. Retained implementation-mutating evidence with no
recoverable generation binding at all — a legacy record predating this
capability, or an inconsistent state — fails closed for that target only,
never for unrelated Issues, until it resolves through the existing owner
lifecycle. This handoff covers every supported production origin that can
reach ownership acquisition today, including ordinary invalidation
processing, explicit/single-Issue processing, capacity-refill admission, and
stale-provider (Jules) recovery, which all converge on the same generation-
bound adapter and tombstone rather than keeping independent duplicate-start
safeguards.

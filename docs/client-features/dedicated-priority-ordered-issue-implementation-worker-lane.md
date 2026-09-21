# Dedicated priority-ordered Issue Implementation worker lane

Issue implementation consumes only durable Implementation-lane arrivals that
were classified from current authoritative readiness and exact durable review
evidence. The worker selects current target priority first and durable FIFO
arrival second, preserves arrivals across operational deferral and restart,
and never invokes or waits for semantic review.

Each attempt rebuilds admission from authoritative state before claiming work
and again immediately before dispatch. A changed generation or loss of READY,
readiness, relationship, or revocation authority retires the stale attempt;
operational refusal returns the same arrival to pending competition. Production
dispatch remains responsible for non-review gates and the exact-generation
ownership handoff, whose durable owned record suppresses duplicate wakeups and
restart replay.

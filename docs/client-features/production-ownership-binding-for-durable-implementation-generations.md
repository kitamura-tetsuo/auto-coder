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

A conclusive no-submission disposition may retain a separate generation-bound
continuation receipt. Ordinary processing can consume that receipt after all
readiness, validation, dependency and ownership gates pass, without `--only` or
an additional operator retry. The historical owned-start tombstone remains.
Admission consumes the receipt before dispatch; another preparation failure can
restore it only when every candidate is conclusively not started. Unknown or
accepted provider outcomes cannot create this receipt. The dispatch result must
explicitly confirm non-start with boolean `True`; a merely truthy unknown value
cannot authorize continuation. A controller-observed
workspace preparation failure before the actual client call is not a provider
invocation; a failure during result handoff remains indeterminate.

The `issue.implementation-not-started` trace records the generation, reason and
`provider_started=false`. Regression coverage lives in
`tests/test_implementation_ownership.py` and `tests/test_ci_repair_bootstrap_exemption.py`.

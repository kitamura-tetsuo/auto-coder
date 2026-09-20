# Two-tier review effect delivery

Accepted two-tier review results cross a separate durable external-effect
boundary in `pr_review_effects.py`.  The accepted payload identifies the
repository and PR opening, review role and attempt, audited and target heads,
base provenance, complete Issue Requirements contract, strong-review policy,
finding-set revision, actual reviewer provenance, and every portable finding
field.  Strong and ordinary-closure payloads therefore cannot collapse into a
shared PASS marker or lose an unanchored finding.

Every publication, repair handoff, or finding-thread transition has an
operation identity derived from that complete accepted payload together with
its purpose and authenticated destination.  The operation is reserved under a
file lock before transport begins.  A competing controller observes the
reservation but cannot send it, while a different accepted round, payload,
destination, or purpose receives a different identity.

Transport outcomes are persisted as `CONFIRMED`, `REJECTED`, `UNCERTAIN`, or
`RETIRED`.  An uncertain operation is reconciled at its exact destination and
is never blindly resent; only a positively rejected operation can retry under
the same identity.  Confirmation records the destination receipt independently
of the immutable accepted payload.  Restart reconstructs both from the atomic
journal.  Before a first send or a retry, the caller must revalidate current
result and destination authority; stale work is rejected without mutation.

This boundary intentionally does not run reviewers, infer ordinary
dispositions, discover cloud ownership, create provider tasks, or merge a PR.
Production adapters remain responsible for authenticated GitHub publication,
authoritative existing-work lookup, provider-specific reconciliation, and real
controller-owned thread transitions; each adapter supplies its observable send
and reconciliation result to the common journal.

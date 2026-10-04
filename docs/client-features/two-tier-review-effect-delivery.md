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

Accepted ordinary closure now resolves the original strong-review finding
threads whose evidence-backed dispositions are `FIXED` or `INVALID` for the
closure head. The controller uses the immutable confirmed strong-publication
identity to locate the exact authenticated root, including after mutable finding
statuses change. The native root must also be observed through the authenticated
reviewer adapter. Missing publication receipts remain pending. Unrelated threads and
unaccepted `OPEN` or `INCONCLUSIVE` proposals cannot authorize resolution.

Closure publication and each thread resolution have separate durable operations.
Resolution records the disposition evidence in a reply and rechecks accepted
cycle authority and the strict current PR head before mutating GitHub. A changed
head during resolution reopens the thread. Missing roots, incomplete observations,
or failed replies/mutations leave closure bookkeeping pending. A later pass
reconciles the exact thread before retrying, without publishing the confirmed
closure review again or invoking an additional reviewer. Closure acknowledgement
waits for all selected finding threads to be confirmed resolved.

The existing `pr.review-thread-closure` dashboard stage exposes confirmed and
unfinished counts, thread IDs, phases and reasons with
`effect="accepted-ordinary-closure"`. These facts distinguish accepted validation
from completed GitHub effects and merge authority.

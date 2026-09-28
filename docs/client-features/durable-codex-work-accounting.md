# Durable Codex work accounting

Ordinary Issue-owned implementation reservations can carry a durable inventory
of Codex operations. Each operation is identified independently of its provider
task ID and retains its source request, causal baseline, execution, publication,
tracking, and settlement evidence. Mutations share the implementation owner's
cross-process serialization and atomically advance the slot activity revision.
Acceptance is a monotonic fact: later ambiguous delivery cannot reclassify
accepted work as definite non-delivery. Settlement evidence must match the
operation's source request, task when bound, and causal baseline when present.

An absent, malformed, or unreconciled inventory is not an empty inventory and
cannot authorize retirement. Complete-empty initialization is limited to fresh
admissions without earlier activity, or to a receipt that identifies a complete,
consistent enumeration of durable sources and their correlated operation IDs.
Reconstruction receipts also retain a non-empty consistency identity for every
required production source. The accounting reader rechecks those identities;
any read failure, addition, removal, rebinding, or mutation makes the inventory
unreconciled until a new complete reconstruction succeeds. Reconstruction
uses two complete observations and refuses a receipt when a source changes
between them. The required inventory includes CloudRun reservations and
bindings, owned retry authorization and dispatch, follow-up delivery records,
repair admission, and initial-PR recovery records.
Malformed operation or settlement evidence makes the snapshot unavailable.
Retirement consumers receive an
immutable snapshot and a guard that holds owner and store serialization through
their removal boundary; foreign-repository, stale, or unsettled snapshots are
non-releasable.

This authority does not submit provider work, grant retries, inspect production
provider sources, change GitHub state, or enable Codex retirement.

The production fence accepts only an authoritative repository, Issue owner,
reservation incarnation, logical operation, source request, optional task, and
causal baseline. It registers that operation under the implementation owner's
serialization before calling transport. A stale/retired incarnation,
conflicting identity, incomplete accounting inventory, or replay fails closed;
a replay therefore cannot spend another POST. Delivery is projected without
equating provider acceptance with execution/publication/tracking completion,
and ambiguous delivery remains unresolved.

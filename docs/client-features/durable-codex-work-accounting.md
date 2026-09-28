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
Malformed operation or settlement evidence makes the snapshot unavailable.
Retirement consumers receive an
immutable snapshot and a guard that holds owner and store serialization through
their removal boundary; foreign-repository, stale, or unsettled snapshots are
non-releasable.

This authority does not submit provider work, grant retries, inspect production
provider sources, change GitHub state, or enable Codex retirement.

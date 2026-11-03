# Durable Codex work accounting

Ordinary Issue-owned implementation reservations can carry a durable inventory
of Codex operations. Each operation is identified independently of its provider
task ID and retains its source request, causal baseline, execution, publication,
tracking, and settlement evidence. Mutations share the implementation owner's
cross-process serialization and atomically advance the slot activity revision.
Owner and store locks use a canonical, process-local coordination registry shared
by repository instances. Reentry on the owning thread retains the outer file
lock, including when a Codex client constructs a fresh repository inside the
engine's owner lock. Other threads and processes remain excluded. Acquisition
waits at most 30 seconds and raises `ImplementationSlotUnavailable` before
transport when the lock cannot be acquired; it never discards ownership to send.
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

Production initial submission now holds that owner fence across the durable
CloudRun claim and CLI creation. Codex Cloud continuations (including repair
callers that share the client) resolve their Issue owner from retained CloudRun
task provenance and hold the fence across follow-up journaling and WHAM. The
initial-PR recovery monitor applies the same boundary around its recovery-store
reservation and direct WHAM request. Untracked tasks remain outside this
Issue-owned policy, while a known task with missing, conflicting, or retired
ownership fails before transport.

Daemon startup reconstructs every active Issue reservation from the production
CloudRun, CloudManager, retry-authorization, RetryDispatch, follow-up, repair,
and PR-recovery stores. Store-native logical snapshots provide consistency
identities for SQLite sources, while file journals use content identities. The
same production readers are invoked before later sends and retirement reads,
so a restart or material source mutation cannot silently preserve a stale
complete receipt.

Reconstruction may bind an unbound pre-send operation to the task ID in its
durable accepted receipt. This recovers retry dispatch acceptance after startup
accounted for the claim but before the provider returned its task ID. The source
request, kind, and causal baseline must still match. An existing task ID cannot
be replaced or removed, and settled or definitely unsent operations cannot gain
a task binding. Binding records acceptance without settling execution or handoff
obligations, so subsequent CI continuations can pass the normal fence without
making the Issue reservation releasable.

Ordinary due reclamation refreshes a complete reconstruction under the existing
owner guard before reading accounting. It can bind the original prepared
follow-up message fingerprint and a verified PR-recovery handoff to that same
logical operation. A fingerprint or PR binding cannot subsequently change or
disappear. Publication and tracking are complete only when the retained
`pr_observed` recovery record has a completed handoff and the same PR is a member
of the current Issue reservation. An unrelated handoff supplies no authority.
The existing-PR repair producers carry their exact repository/PR/head/branch
contract to the sender. Before POST, the sender validates its bounded prompt and
records that contract with the actual Issue/incarnation under the same owner
guard. Reconstruction requires every grouped source record to agree with that
binding. A generic continuation, replacement-publication request or historical
record without this producer binding remains unsupported for handoff completion;
an older task-level receipt cannot retroactively supply the missing contract.
For an unbound historical record, reconstruction retains only a candidate PR
handoff, with publication/tracking still unproven. The observer can establish the
missing operation responsibility from its exact fingerprint-bound native user
input only when that input matches the complete supported existing-PR producer
template (including consistent repository/PR/head/branch fields). An unrelated
or replacement request, an unsupported template or a conflicting partial journal
binding remains unknown. No historical journal is backfilled or relabeled.
The source token includes recovery records and PR membership, and is rechecked
at commit. Reconstruction never treats an unavailable source as an empty one.

Recovery keeps the current journals and retired history. Restore a qualified
worker build using an owned drain/stop and one observed start against the same
current state; never restore an older journal over later activity or clear an
unknown operation to free capacity. If correlation or the producer binding is
unavailable, retain the reservation with its ordinary reevaluation obligation
until independently supported evidence or an explicitly authorized disposition
exists. The repair never infers responsibility from the task-level handoff alone.

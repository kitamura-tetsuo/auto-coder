# Definitive Parent-Issue Refusal Settlement

The durable Issue invalidation worker treats a typed `ParentSpecificationError`
from authoritative relationship reconciliation as a completed BLOCKED evaluation.
The same terminal disposition is carried explicitly by ordinary processing
results; generic BLOCKED results, arbitrary exceptions, and diagnostic text do
not qualify.

Before acknowledging the claimed generation, the worker atomically retires the
target's pending Review and Implementation roles and child Implementation
arrivals that are still bound to it as family parent. Failure of that cleanup
leaves the invalidation recoverable. Closed-target authority revocation remains
a separate prerequisite performed from the strict closed observation.

Acknowledgement uses the queue's generation-aware completion transition. A
newer invalidation remains pending and is enqueued for the running worker, while
a stale claim cannot clear current work. Operational reconciliation errors,
deferrals, cancellation, persistence failures, and an earlier unresolved
processing error retain their existing retry or recovery behavior even if final
routing later discovers a specification refusal.

Typed refusal propagation includes the ordinary live-family refresh after
initial routing. If submitted-parent validation has already failed or been
cancelled, its unfinished disposition remains authoritative when the mandatory
post-validation routing pass subsequently observes a relationship refusal.

A settled refusal emits a BLOCKED worker diagnostic containing repository,
Issue number, claimed generation, relationship reason, and the actual
acknowledgement outcome. It does not emit `worker_error`, schedule the generic
60-second retry, claim READY or successful implementation, or suppress a later
invalidation after relationship correction.

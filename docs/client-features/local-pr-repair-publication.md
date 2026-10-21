# Local PR repair publication

Completed local CI-failure and merge-conflict jobs retain an authenticated result
manifest in their private workspace. `PRRepairJobFinalizer` is the controller-owned
boundary that turns that exact artifact into a commit and publishes it to the
original same-repository PR head. It uses a per-PR process-independent lock, the
workspace checkout lock, and the retained execution identity; it does not invoke an
editor or provider.

Commit, push, and new-head invalidation are separate durable effects. The strict
commit path runs normal Git hooks but never formats, re-stages, or asks a model to
repair a failure. Before its first push, the candidate SHA is durable and the push
intent is atomically inserted only if no prior intent exists. Publication uses an
exact lease against the admitted head SHA.

An interrupted or ambiguous push is recovered by observing the original PR ref.
Only an exact match with the retained candidate confirms publication. Seeing the
old head, a newer head, or an unavailable target never authorizes another push.
After confirmation, the finalizer durably schedules normal processing for the new
head. That wake is not evidence of CI success, review approval, or merge completion,
and settlement retires only the exact local job execution.

This boundary introduces no dashboard-facing event or outcome schema. Its wake
feeds the existing new-head processing path, so the dashboard observability contract
continues to observe the independently evaluated PR/CI lifecycle rather than an
additional publication status.

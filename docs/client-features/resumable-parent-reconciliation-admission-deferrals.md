# Resumable parent reconciliation admission deferrals

Parent, direct-child, and sibling-dependency reconciliation preserves a local
GitHub admission refusal when it is wrapped by `ParentOperationalError`.
Classification follows the typed exception cause and accepts only the supported
`GitHubRequestDeferred` reasons with definitely-not-sent delivery certainty; it
never parses diagnostic text or converts other operational failures into retry
success.

Issue evaluation durably coalesces this unfinished reconciliation in
`PendingWorkStore` before returning `Deferred`. The obligation records the
repository, Issue, current specification revision, original typed diagnostic,
and an effective retry deadline. The normal pending-work scheduler later
refreshes the Issue and repeats the complete current-state admission path, so a
restart or an in-process wake cannot authorize work from partial relationship
evidence. The store's one-second local retry floor and maximum retained deadline
prevent notification-driven busy loops, while every retry still passes through
the GitHub request governor.

Capacity-refill enumeration uses the same durable issue-processing obligation
when relationship reconciliation is refused before sending. The affected Issue
is excluded from that refill pass, its evaluation remains pending, and the
scheduler is awakened for deadline-respecting reevaluation rather than relying
only on an in-memory retry flag. Later refill passes leave that retained Issue to the
pending-work scheduler, so capacity polling and unrelated slot transitions
cannot shorten the retained deadline.

Candidate dispatch applies the same conversion to native-parent discovery and
the later hierarchy rechecks, including discovery repeated after entering the
per-Issue generation lock. A definitely-unsent refusal therefore yields the
lock and returns a durable Deferred result to capacity refill; it cannot escape
through the synchronous worker boundary and terminate the refill service.
The initial strict target snapshot in common candidate dispatch uses this same
typed boundary. A supported Governor refusal there is retained against the
candidate's carried title/body revision before returning `Deferred`; unsupported
refusal categories continue through the ordinary failure path rather than
gaining timed-retry authority.
The final relationship/generation freshness check before ownership admission
and a retained owner's submitted-family recheck use this boundary as well, so
neither can silently downgrade a refusal to stale evidence or lose the pending
evaluation after earlier validation succeeds.
The common dispatch wrapper provides the same typed retention fallback for
strict snapshots repeated under owner serialization and immediately before
implementation dispatch. This prevents a later bare refusal from escaping the
refill task or being flattened into a non-durable retry error.

Automatic worker and capacity-refill intake consults a retained admission
deadline before entering the target's strict reader. Duplicate notifications
and unrelated capacity wakes therefore leave the retained deadline unchanged;
once it is due, the pending-work handler performs the normal strict refresh and
common admission path. An unsuccessful resumed evaluation does not acknowledge
its unfinished effects merely because common processing returned a result.
Typed authentication failures replace the expired admission reason with the
existing authentication operational block, while malformed or other evaluation
failures become a non-automatic evaluation block; neither inherits timed retry
authority from the earlier definitely-unsent refusal.

Durable-invalidation workers apply the same deadline gate immediately after
claiming a duplicate notification, before dependency observations, target
refresh, stage routing, or common dispatch. The notification is retained at the
pending-work deadline without changing the pending obligation.

Durable-invalidation stage routing also recognizes an explicitly caused
reconciliation deferral and stores its reason, API origin, and effective retry
deadline on the invalidation record. Only explicit reconciliation cause chains
are eligible; implicit or suppressed exception context is not treated as
deferral authority.

Deferral diagnostics identify the repository, Issue, interrupted stage, original
reason, API origin, deadline, and delivery certainty at warning level. A durable
write failure remains fatal to that evaluation and no dependent implementation
or validation is authorized.
Ordinary workers report a successfully retained refusal as a deferred Issue at
info level and in the worker trace, rather than emitting the generic processing
failure diagnostic.

Typed GitHub transport failures in an explicit `ParentOperationalError` cause
chain are also retained as unfinished Issue evaluation, with the original delivery
certainty. An indeterminate delivery remains an operational block in the existing
pending-work store; it is not relabeled as a definitely-unsent admission refusal
or automatically retried as a successful relationship read. Diagnostic text and
implicit exception context are never used to classify the failure.

The common candidate-processing boundary catches operational reconciliation
errors escaping later hierarchy checks and returns `Deferred` with refill retry
required, preventing one unavailable parent read from terminating the worker.
Untyped operational failures retain that retry result without inventing a typed
durable obligation. Transport failures emit the existing
`issue.parent-reconciliation` stage with a deferred outcome, reason, and delivery
certainty; the dashboard shows the evaluation as deferred, never completed.

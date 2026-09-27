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
is excluded from that refill pass, the refill remains pending, and the scheduler
is awakened for deadline-respecting reevaluation rather than relying only on an
in-memory retry flag.

Deferral diagnostics identify the repository, Issue, interrupted stage, original
reason, API origin, deadline, and delivery certainty at warning level. A durable
write failure remains fatal to that evaluation and no dependent implementation
or validation is authorized.

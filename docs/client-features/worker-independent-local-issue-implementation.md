# Worker-independent local Issue implementation

Eligible synchronous local Issue implementations are transferred from the
ordinary dispatch claim into `LocalJobStore` before the editing backend is
entered. The dispatch result is `local_accepted`, which means durable,
in-progress ownership rather than completed implementation. The Issue worker
can acknowledge its invalidation and serve another candidate while the local
runner retains the Issue attempt and its logical implementation slot.
Daemon invalidations and explicit `process-issues --only` dispatch lazily open
the same durable authority, so an explicit route does not fall back to a
synchronous editing call merely because `start_automation` was not entered.

The daemon polls the durable store on a runner-owned executor lane. Immediately
before provider entry, the Issue adapter strictly refetches the open Issue,
reapplies the normal readiness, family, specification, dependency and author
authority path, verifies the exact dispatch incarnation and attempt, and
requires the original implementation owner to remain active. Refusal returns
an unentered claim to pending; it never falls through to another provider.
Authorization also recomputes the current contract generation against the
generation retained by the exact active execution. Workspace preparation does
not count as provider entry: the adapter repeats this authorization immediately
before the editing call and only then checkpoints `provider_entered`.
Accepted work executes in its job-owned private clone. Its immutable result
manifest is checkpointed by the runner and routed to `IssueJobFinalizer`, which
owns commit, push, Issue-to-PR association and settlement. Restart discovery
uses the same job identity and does not require a new GitHub webhook.
Private roots are selected through the command-execution context rather than
process-wide `chdir`, so separate runner slots may enter their backends
concurrently. Backend construction remains a synchronous pre-handoff boundary:
a positively unavailable CLI or other confirmed-not-started preparation
releases the dispatch claim and advances the existing ranked fallback policy;
only successful preparation may create the durable local job.

The outer Issue dispatch deliberately does not finish an execution whose result
is `local_accepted`. Active Workers therefore reports the released queue worker,
while implementation-slot and durable-job projections continue to report the
in-flight implementation until finalization settles it.

Regression coverage includes the durable dispatch boundary in
`tests/test_cloud_backend.py` and the store, runner, workspace and finalizer
suites.

# Durable local-job continuation evidence

`LocalJobStore` retains continuation identity and progress for accepted Issue
implementation and PR correction jobs without treating the end of a foreground
provider turn as invocation completion. A continuation binds the original job,
execution incarnation, prepared workspace, backend input, and provider session.
Provider sessions cannot be rebound to another job.

The continuation read model distinguishes foreground work, background waiting,
pending continuation, reconciliation, confirmed final output, interruption, and
failure. Task observations have stable identities and a monotonic incorporated
result frontier. Missing inventory does not remove task evidence, terminal task
observations cannot be rolled back by older running observations, and candidate
output can be confirmed only at the current frontier after all recorded tasks
are terminal.

Every mutation is fenced by the current executor token. Recovery ownership can
change only through an explicit compare-and-replace takeover; reopening the store
or observing a later clock does not grant authority. Controller commands are
persisted before submission with stable payload and causal-frontier identities.
Their delivery knowledge distinguishes not-submitted, admitted, indeterminate,
and terminal states, and native provider continuation remains distinct from a
controller command.

Continuation discovery is independent of ordinary new-job claim eligibility and
survives database reopen. Databases and jobs created before this capability have
no synthetic continuation state. This store remains an evidence boundary only:
reads and replays do not call a provider, recreate a workspace, allocate another
attempt, publish, release domain ownership, or settle downstream effects.


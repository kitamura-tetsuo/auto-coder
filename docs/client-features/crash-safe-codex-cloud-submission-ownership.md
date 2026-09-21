# Crash-safe Codex Cloud submission ownership

Initial Codex Cloud dispatch publishes a repository- and Issue-attempt-scoped
durable claim before invoking the remote-capable CLI. Claims retain the named
backend, effective environment, base branch, delivery outcome, and provider
task identity/URL when known. Accepted and indeterminate claims suppress
redispatch across workers and restarts. Accepted tasks are reported as handed
off only after the authoritative run journal and `cloud.csv` ownership
projection agree; missing projections are repaired without another submission,
while corrupt or contradictory ownership fails closed for operator attention.
Explicit owned retries may replace only the predecessor captured by their
durable creation claim. Replay confirms or repairs the same receipt without a
second provider call and non-destructively preserves the accepted run's
environment, branch, URL, outcome, and accumulated pull-request associations,
even when current backend configuration has changed.

# Issue implementation execution service

`IssueImplementationService` is the single Issue-only boundary from durable
implementation ownership through provider dispatch and execution settlement.
The automation controller supplies the already-authorized generation plus
narrow freshness, dispatch, diagnostic, and trace ports; the service does not
own specification review, family/dependency validation, provider selection, or
pull-request processing.

Every entry that reaches ordinary Issue implementation uses this boundary,
including the implementation lane, capacity refill, explicit targets, and
unlocked or stale recovery. The service repeats the authoritative generation
check before ownership and after capacity reconciliation, keeps owner
serialization across provider entry, and stops new entry after graceful drain.
Explicit retry admission remains bound to its durable request, attempt,
generation, and execution rather than acting as a general force override.
Daemon recovery of a timed-out Jules session also re-enters this boundary with
its captured generation while retaining the dedicated high-score fallback
policy; it does not join the ordinary provider pool.

Settlement follows the actual execution boundary. A local accepted handoff
retains its execution for the local job, inherited executions remain owned by
their caller, and other returning executions finish only their exact ID. A
confirmed non-start is retryable, but the logical owner is released only after
both remote run and binding stores authoritatively show no provider ownership.
Accepted or unreadable remote evidence therefore never becomes an idle-release
signal. Existing dispatch and lifecycle trace stages are emitted through the
supplied ports, so the production-to-dashboard schema and meanings are
unchanged.

Focused routing, race, retry, transfer, and settlement regressions are in
`tests/test_issue_implementation_worker.py` and
`tests/test_implementation_ownership.py`.

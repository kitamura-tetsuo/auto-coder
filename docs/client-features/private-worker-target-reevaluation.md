# Private worker target reevaluation

An operator using the opt-in Unix-domain worker-management socket can request
ordinary reevaluation of one Issue or pull request with `POST
/v1/reevaluations`. The closed request schema contains an opaque operation ID,
the configured repository and channel, the expected process-run and deployment
generation, and an `issue` or `pr` target with a positive integer number. It
does not accept commands, paths, provider choices, snapshots, force, retry,
resume, approval, or gate-bypass options. No equivalent route is exposed by the
public HTTP application.

The worker accepts a new request only after successful startup reconciliation
while the captured incarnation is serving and RUNNING. Acceptance atomically
stores an immutable operation receipt with the ordinary durable invalidation
transition. An identical operation is a read-only replay; changing its bound
payload is a conflict. `GET /v1/reevaluations/{operation_id}` reads this durable
receipt, so an operator can reconcile a lost or timed-out response after a
disconnect or restart. Missing authoritative evidence is reported as not found,
while unreadable storage is unavailable rather than fabricated success.

The receipt outcome is only `accepted_for_reevaluation`. The existing worker
still refreshes authoritative GitHub state and applies dependency, ownership,
retry, CI, review, and publication gates. Reevaluation is not forced retry or
resume, and restarting a worker does not grant retry authority. It neither
pauses nor terminates external provider tasks. Pending targets coalesce and an
in-flight target receives the queue's single normal follow-up generation;
event-loop wakes remain recoverable hints backed by startup recovery.


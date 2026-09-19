# Durable explicit Issue-review reruns

An explicit rerun is a durable operation over a nonempty, normalized set of
stable review subjects. A subject is either an individual Issue or a parent
decomposition in one repository. The request identifier is permanently bound
to its exact set: exact replay is idempotent, conflicting reuse is rejected,
and a later request supersedes the earlier authority only for overlapping
subjects. Acceptance uses one fully synchronous SQLite transaction, so a
reported success means every selected subject was revoked and survives a
controller restart.

Rerun authority is deliberately independent of semantic validation identity.
Lifecycle decision reads compare the stored occurrence authority with the
latest stable-subject authority, and therefore exact text or policy reversion
cannot restore a pre-request decision. Scheduler coalescing includes the
authority sequence, preventing fresh work from joining a Future admitted
before acceptance. A running old invocation may finish, but its final
authority check converts it to a retryable error before persistence or
handoff. Decision-store reads use the same check, covering direct admission
consumers as well as the Review worker lane.

Each request exposes per-subject `pending`, `deferred`, `satisfied`, or
`superseded` state. Deferred state retains a concrete reason. Satisfaction
requires a terminal decision persisted under the still-current authority and
records its semantic decision reference plus whether it came from model
execution or a deterministic local-only evaluation. Cache removal, enqueue,
transport success, stale completion, and `ERROR` never satisfy the request.
The journal is fail-closed: an unreadable authority database raises an
availability error rather than making an old decision reusable.

The operation does not delete validation reports, Objective anchors,
baselines, publication receipts, repair state, reissue stops, or
implementation ownership. It neither changes implementation generation nor
cancels already-owned implementation work. The production operation performs
authoritative current-state admission immediately after durable acceptance.
It creates a durable Review-lane arrival without invoking the reviewer, or
records a concrete deferred reason when readiness, category, stabilization,
or family reconciliation prevents fresh review. Startup recovery enumerates
pending and deferred request subjects and repeats that same admission pass,
so a crash after acceptance but before the initial wake cannot orphan the
request.

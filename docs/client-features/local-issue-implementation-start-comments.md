# Local Issue implementation start comments

When an authorized local Issue implementation reaches BackendManager's actual
editing-client call boundary, Auto-Coder posts a GitHub Issue comment identifying
the selected backend alias, selected model (or `unknown`), and the durable local
job ID. The comment explicitly describes controller dispatch rather than model
success, edits, tests, publication, or completion. Read-only calls, PR work,
cloud dispatch, admission, workspace preparation, backend construction, and the
earlier `provider_entered` checkpoint do not produce this comment.
Both durable runner handoffs and the supported synchronous ordinary-Issue path
bind the same actual client boundary. The synchronous path derives its stable
identity from the dispatch guard's implementation attempt and retains delivery
in the shared local-job database without requiring a model reinvocation.

The local job database retains the observed start and its delivery state
independently of job completion. A deterministic hidden marker derived from the
attempt-bound job ID supplies exact remote evidence. Transactional claiming
allows only one publisher to move a pending or definitely-unsent notification
into an indeterminate in-flight state. A successful POST records confirmation;
an ambiguous POST remains indeterminate and is only confirmed by a fresh strict
Issue-comment read containing that exact marker. Missing or unavailable evidence
never authorizes another POST. A failure known to precede transmission remains
definitely unsent and may be retried after restart, including after job
settlement, without invoking the model again.
Typed GitHub admission deferrals whose request outcome is definitely not sent
remain in that retryable state; recovery may POST them once the governor permits
traffic. Claim, recovery-enumeration, and receipt-write failures emit explicit
diagnostics while leaving their prior durable delivery knowledge unchanged.

Notification storage, POST, and reconciliation failures are diagnostic side
effects only. They cannot stop or reclassify the local invocation, consume a new
implementation slot, reopen a job, or alter publication and settlement. The
comment deliberately avoids the attempt manager's `Auto-Coder Attempt:` and
numeric `Attempt #N` forms, so it does not increment Issue attempts or authorize
another dispatch.

Regression coverage is in `tests/test_local_issue_start_notification.py` and
exercises the actual local-client observation boundary, negative read-only and
pre-boundary cases, exact-marker ambiguity reconciliation, unavailable reads,
post-settlement recovery, attempt-parser isolation, and a barrier-synchronized
transactional publisher contest.

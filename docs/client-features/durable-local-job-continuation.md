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

An exact recorded `failed` or `interrupted` invocation can finish without a
successful assistant response. Before making its downstream failure disposition
eligible, the runner positively verifies that the retained host lifetime and
every live member of its process group have ended, then checkpoints the matching
negative continuation state. A dead group leader alone, missing writer identity,
or unreadable process evidence cannot authorize settlement. The original result
artifact, session, task history, and attempt remain intact; no final assistant
output or successful background-task result is manufactured. This also recovers
older jobs stranded at `result_recorded` after a Muse timeout, including when the
provider workspace has already been disposed. The finalizer releases only the
transferred active execution and retains normal implementation-owner policy.

Recovery of a result artifact or downstream checkpoint uses the continuation's
current executor token, which can differ from the original runner owner after
takeover. Stale tokens and replaced execution incarnations remain fenced. An
expired invocation budget cannot prevent settlement of an already recorded
failure; it still forbids a new automatic provider turn.

New durable Muse Issue timeouts/interruption checkpoints use `paused` with no
terminal artifact, retaining the provider repository for explicit same-session
resumption. The negative-result recovery above continues to apply to genuine
terminal failures and previously recorded artifacts. See
`explicit-local-issue-session-resume.md` for the continuation contract.

Run `bash scripts/test.sh tests/test_local_job_runner.py
tests/test_local_job_continuation.py` for timeout/interruption recovery, incomplete
result checkpoints, live/unknown writers, descendant writers, and executor fences.
`tests/test_explicit_local_issue_completion.py::test_explicit_issue_waits_for_local_execution_and_finalization[continuation_timeout]`
also crosses the production adapter, private workspace, runner, trace, and mounted
dashboard view, asserting paused/deferred status without publication or another
backend call. `tests/test_issue_interruption_resume.py` verifies that preserved
edits subsequently reach successful publication through the same session.


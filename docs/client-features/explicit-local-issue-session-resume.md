# Explicit local Issue session resumption

`auto-coder process-issues --repo owner/repo --only 123 --resume` continues a
retained local Issue implementation rather than requesting a distinct attempt.
`--resume` requires `--only`, rejects PR targets, and cannot be combined with
`--retry`. It does not require `--force`; that global flag retains its separate
process-lock meaning.

The command selects the exact job bound to the active implementation execution.
A sole historical job can be inspected when no active binding exists; ambiguous
history is refused. Resume never enters ordinary Issue dispatch, reserves another
implementation slot, changes the attempt, prepares another clone, selects a
fallback backend, or starts a fresh provider session. Completed results use the
existing job finalizer to replay only missing publication effects.

Unfinished provider-session resumption currently supports Muse, including named
Muse aliases. The actual MSP workspace and host process lifetime are persisted
at session confirmation. Both the previous runner and host must be positively
dead, with no live member of the host's process group. Missing/unavailable roots,
changed source HEAD, unknown process ownership, missing session bindings, stale
Issue/family/dispatch authority, and unsupported providers are explicit refusals.
Older jobs without the actual provider-root/writer binding cannot safely resume.
Already recorded failure/interruption results retain their terminal disposition;
this option does not turn them into another implementation attempt.

An atomic claim replaces the executor token and runner owner while preserving
the job, attempt, execution incarnation, implementation owner, and workspace.
Concurrent commands and delayed writes by the prior executor cannot authorize
another turn or publish stale output. Automatic takeover also compares its
observed runner owner, so a recovery scan captured before an explicit claim
cannot steal the newly admitted turn. An explicit command authorizes one new
continuation turn with the captured backend's configured timeout, recorded
durably. Automatic crash reconciliation still consumes its original deadline.

Muse must confirm the same session, workspace, model, and unattended approval
policy. The original implementation prompt accompanies the continuation, with
instructions to inspect retained edits and complete the task and relevant tests.
Only a confirmed final response after writer settlement can hand files back to
the retained publication workspace and enter the ordinary result/finalizer path.
Failure preserves the retained workspace and a reconciliation reason.

Historical live-inventory rows can outlive their earlier Muse turn. They are
never removed or marked successful merely because the resumed host reports an
empty inventory. After writer settlement, the adapter can reconcile a retained
candidate against the native session journal selected by Muse's session index.
The exact session and provider root must match, every unresolved inventory-only
predecessor must have a positive terminal record before the candidate's turn
began, and the exact later assistant message, completed turn, and clean final
shutdown must agree with the captured current replay. Work with missing result
delivery, an explicit undelivered result reference, a later turn, contradictory
or incomplete evidence, or a changed executor/frontier remains unresolved.
Transport completion retains an unknown domain outcome rather than fabricating
tool success. Task reconciliation and candidate confirmation commit atomically.

Native `omitted_live_only` markers for schema-v1 ephemeral
`task_tool_delta_v1` output deltas are accepted after validating their session,
stream position, and complete omission classification. These entries have no
ordinary payload or timestamp; they do not replace task terminals or assistant
records. Unknown markers, durable or terminal omissions, malformed positions,
foreign sessions, and missing required terminal evidence still refuse recovery.
The 174-predecessor regression includes the 375 omitted-delta markers observed
in Issue #5629's retained journal.

This reconciliation runs at the live Muse result boundary and before an explicit
command submits another turn. A recoverable candidate is checkpointed and handed
to the finalizer without another model call. Regression coverage in
`tests/test_muse_retained_result.py` includes 113 retained predecessors, a live
protocol-host continuation, refusal controls, and the mounted Issue detail view.

The production trace emits `issue.local-session-resume` under the requested Issue,
with its actual completion or deferred outcome. Publication retains the existing
`issue.local-publication-finalization` event. Run `bash scripts/test.sh
tests/test_explicit_issue_resume.py tests/test_explicit_local_issue_completion.py
tests/test_local_job_runner.py tests/test_local_job_continuation.py
tests/test_muse_msp.py tests/test_process_issues_cloud_only.py` for the bounded
regressions, including the real protocol-host continuation and mounted detail view.

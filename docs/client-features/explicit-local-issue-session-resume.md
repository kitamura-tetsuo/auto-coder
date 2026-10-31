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

New durable Muse Issue invocations preserve their actual provider repository under
the job's execution directory. A timeout or controller interruption with a known
session and positively stopped writers checkpoints `paused`, retaining edits,
session, attempt, execution incarnation, and implementation ownership without
creating a terminal failure artifact. `--only <issue> --resume` continues that
same session in that same repository; repeated interruptions remain resumable.
Typed provider usage limits and retryable transport interruptions follow the same
retention path when the session/root and writer-settlement evidence are available.
A paused job may resume while its original daemon is alive because its stopped
writers and paused-state compare-and-replace fence prevent concurrent execution.
Running jobs still require the previous controller to be dead. A hard controller
crash leaves its durable root available to the existing dead-owner resume path.
The controller never automatically resets the original timeout budget or starts
a replacement implementation. `--retry` remains a distinct fresh attempt.

Ordinary non-timeout failures and legacy terminal artifacts retain their existing
disposition. A workspace already deleted by an older controller cannot be restored
from a session identifier alone. Missing roots, live/unknown writers, and changed
Issue authority continue to refuse resumption without fresh-session fallback.

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
MSP pipe readiness supports high-numbered open file descriptors without changing
the invocation deadline, stderr draining, or identity and failure checks.
Only a confirmed final response after writer settlement can hand files back to
the retained publication workspace and enter the ordinary result/finalizer path.
Failure preserves the retained workspace and a reconciliation reason.

After process loss, Muse may report its orphaned predecessor's cancellation while
the new turn is being admitted, or steer the resumed `turn/start` command into
a native background-terminal delivery run that started on `session/resume`. The
transport correlates an orphaned terminal only to the exact predecessor recorded
for the same job/backend/attempt/session and workspace, and binds a steered
`turn/start` acknowledgement (`startedNewTurn: false`) to the active session run
already opened on resume. Unknown turns and foreign sessions still fail; the
admitted turn must produce its own confirmed terminal and assistant output.

Historical live-inventory or session-cursor auto-backgrounded rows can outlive
their earlier Muse turn. They are never removed or marked successful merely
because the resumed host reports an empty inventory. After writer settlement,
the adapter can reconcile a retained candidate (including a steered resume run
correlated via `user_steer` causation in the session journal) against the native
session journal selected by Muse's session index. The exact session and provider
root must match, every unresolved predecessor must have a positive terminal
record (`completed` or `cancelled`) before the resumed invocation's foreground
turn began (and any in-turn managed session cancelled at host shutdown must be
recorded before clean `session_end`), and the exact later assistant message,
completed turn, and clean final shutdown must agree with the captured current
replay. Work with missing result delivery, an explicit undelivered result
reference, a later turn, contradictory or incomplete evidence, or a changed
executor/frontier remains unresolved. Transport completion retains an unknown
domain outcome rather than fabricating tool success. Task reconciliation and
candidate confirmation commit atomically.

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

`bash scripts/test.sh tests/test_issue_interruption_resume.py` exercises timeout,
repeated timeout, and a killed controller followed by a reopened store and the real
MSP transport. Each case asserts one session start, exact-session resumption,
preserved partial edits, unchanged attempt/incarnation, and publication of both
the original and continued edits to a real local Git remote.

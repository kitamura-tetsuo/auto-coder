# Durable local job runner

`LocalJobRunner` owns a finite executor lane independently of Issue and PR
workers. Calling `poll()` only claims and schedules accepted `LocalJobStore`
envelopes; it never waits for inference. When capacity is full, further jobs
remain durably pending. `capacity` is mandatory and must be at least one.

Each job kind has a registered domain adapter. The adapter rechecks current
authoritative provider-entry permission for the exact claimed incarnation.
Every runner requires an explicit repository. Discovery, claim selection,
checkpoint recovery, dead-owner reclamation, and downstream notifications are
restricted to that repository and its registered job kinds, intersected with
any exact-job scope. Controllers sharing the local-job database therefore never
claim or reclaim another repository's jobs, including when that controller's
PID cannot be resolved from the observing container. An older foreign job cannot
consume a runner slot or starve the repository's own pending work.
If external recovery returns a row to pending while this runner's pre-entry
future is still active, polling leaves it pending until that future exits.
It never replaces the active future or creates duplicate admission handles.
Missing, failing, or negative authorization returns a positively unentered job
to pending and cannot invoke a provider. Before calling an authorized adapter,
the runner durably records its owner and provider entry. A running record with
missing or ambiguous owner liveness is therefore reconciliation work and is
never automatically reclaimed or replayed. Default owners bind a PID to its
Linux process-start token. On restart, an unentered claim is returned to pending
only when that exact process lifetime is authoritatively dead; opaque ownership
or unreadable liveness remains suppressed.

The adapter receives the persisted repository, backend alias, immutable input,
origin, and execution incarnation. Actual output (including failures) is bound
to that incarnation as a result artifact, recorded as the invocation result,
and moved to `downstream_effects_pending` before a completion wake is emitted.
Wake replay only rediscovers durable downstream work and cannot claim the job
or invoke the model again. `wake_downstream()` schedules this replay on the
runner-owned notification executor, and every later `poll()` also schedules
eligible notifications without running consumer callbacks on its caller. A
persisted `result_recorded` incarnation is validated against its exact result
artifact and advanced to downstream eligibility after restart, without provider
re-entry. A provider-entered running incarnation with a committed artifact is
likewise reconstructed into its result checkpoint before the downstream
transition. Same-process recovery retains and settles the original invocation
admission handle only after downstream eligibility commits, so graceful drain
does not wait on work whose minimal durable checkpoint is complete. Empty
provider output remains an actual checkpointed result; provider exceptions and
interruptions are recorded as distinct outcomes.

Durable Muse Issue timeouts and interruptions are resumable when their session,
provider root, and positive writer settlement are available. The runner instead
checkpoints `paused`, settles the invocation admission handle, retains the exact
implementation execution, and emits `issue.local-session-pause` with DEFERRED.
No result artifact, downstream wake, or publication is authorized by pausing.
Ordinary polling leaves paused work retained for an explicit `--only --resume`;
its atomic claim changes the executor token and returns the same job to running.
Repeated pauses and controller-crash recovery preserve the original attempt and
files. `tests/test_issue_interruption_resume.py` covers the production path.

The daemon's `InvocationAdmissionGate` is checked before a claim. Graceful
drain therefore leaves queued work pending while protecting already-admitted
provider calls through their result checkpoint. A failed result or downstream
checkpoint remains visibly unfinished and keeps its invocation admission handle
unsettled. A committed result is recoverable as described above, while a failed
result-artifact checkpoint remains running for reconciliation; forced shutdown
does not manufacture completion.

Regression coverage is in `tests/test_local_job_runner.py`.
Repository isolation regressions cover foreign pending jobs, pre-entry owners,
result artifacts, result checkpoints, and downstream notifications, plus a live
pre-entry authorization barrier whose owner PID is unavailable to a foreign
repository runner.

An optional immutable `job_ids` scope restricts execution, checkpoint recovery,
and downstream notifications to those exact envelopes. Explicit single-Issue
commands use this scope and wait for their runner before invoking the finalizer.
`recover_unsettled()` reconciles checkpoints without scheduling provider calls.

Every unsettled local job, including Issue implementation, PR CI/conflict repair,
and PR review correction, retains its exact implementation execution independently
of the accepting controller's PID. Stale-process reclamation strictly reads the
durable job authority before removing an execution; an unavailable job database
defers reclamation. A completed job whose unretired owner was lost can restore
that same owner incarnation for publication after a fresh Issue/family contract
matches its captured generation. Replacement owners, retired incarnations,
changed contracts, or unavailable contract evidence cannot be overwritten.
This recovery never admits another model invocation. Coverage is in
`tests/test_local_issue_owner_recovery.py`.
PR controller-death and settled-job reclamation coverage is in
`tests/test_local_pr_repair_recovery.py`.

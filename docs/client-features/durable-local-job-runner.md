# Durable local job runner

`LocalJobRunner` owns a finite executor lane independently of Issue and PR
workers. Calling `poll()` only claims and schedules accepted `LocalJobStore`
envelopes; it never waits for inference. When capacity is full, further jobs
remain durably pending. `capacity` is mandatory and must be at least one.

Each job kind has a registered domain adapter. The adapter rechecks current
authoritative provider-entry permission for the exact claimed incarnation.
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
re-entry. Empty provider output remains an actual checkpointed result; provider
exceptions and interruptions are recorded as distinct outcomes.

The daemon's `InvocationAdmissionGate` is checked before a claim. Graceful
drain therefore leaves queued work pending while protecting already-admitted
provider calls through their result checkpoint. A failed result or downstream
checkpoint remains visibly running and keeps its invocation admission handle
unsettled; forced shutdown does not manufacture completion.

Regression coverage is in `tests/test_local_job_runner.py`.

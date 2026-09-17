# LLM invocation admission and shutdown protection

`src/auto_coder/invocation_admission.py` defines a daemon-instance-scoped
model that protects individual LLM invocations from a graceful shutdown,
distinct from the coarser thread-ownership wrapper described in
[Graceful daemon shutdown](graceful-daemon-shutdown.md)
(`AutomationEngine._run_local_critical`, which currently treats a whole
worker or maintenance task as critical). `AutomationEngine` owns one
`InvocationAdmissionGate` per daemon lifetime (`self.invocation_gate`),
installs it ambiently alongside the existing `install_admission_check` inside
`_run_local_critical` (so it reaches every worker/validation-executor/
capacity-refill/maintenance/durable-resumption call that already runs through
that boundary's `asyncio.to_thread`), closes its admission in
`request_graceful_shutdown`, and forces it in `request_force_stop`. Retiring
the broader `_wait_for_local_critical_operations` wait in favor of this gate
is a separate follow-up (#2010): today this gate only tracks invocations and
never gates the daemon's own exit, so the coarser wrapper remains the
production wait set.

## Wired production boundaries (Issue #2009)

`BackendManager._admit_invocation`/`_settle_admitted_invocation` wrap the
single shared final invocation boundary inside
`_execute_backend_with_providers` (the actual `cli._run_llm_cli(...)` /
`cli.continue_session(...)` call), so every one of `run_llm_prompt`,
`run_llm_noedit_prompt`, `run_prompt`, explicit `continue_session`, automatic
session-resume fallback, and every backend/provider rotation retry admits and
settles its own invocation without any call-site change. When no gate is
installed (a standalone command), `_admit_invocation` returns `None` and
behavior is exactly as before.

A caller classifies the invocation(s) it is about to make with
`bind_invocation_target(repository, target, stage, defer_checkpoint=False)`
(an ambient `ContextVar`, mirroring `install_invocation_gate`). By default
(`defer_checkpoint=False`) a successful invocation settles immediately after
the provider call returns, because the response flows synchronously to a
caller with no separate durable write to protect. A caller that owns its own
durable checkpoint — a persisted validation decision, a recorded remote
handoff receipt — binds `defer_checkpoint=True`; `_settle_admitted_invocation`
then stashes the handle via `set_pending_invocation_handle` instead of
settling it, and the caller retrieves it with `take_pending_invocation_handle`
once its own write commits, calling `confirm_settled()` on success or
`record_checkpoint_attempt_failed(...)` (leaving it unsettled and retriable)
on a write failure. A failed invocation (any exception from the provider
call) always settles immediately regardless of `defer_checkpoint`, since a
failure has no reusable result to protect.

Wired callers:

- `SpecificationValidationLifecycle.decide()` / `DecompositionValidationLifecycle.decide()`
  bind `defer_checkpoint=True` around their analyzer call and settle only
  after `store.save()` of a READY/BLOCKED decision confirms (or immediately,
  for an ERROR decision that is never cached).
- `jules_engine.check_and_start_recurrent_jules_tasks` admits before
  `jules_client.start_session(...)` (a local submission of asynchronous
  remote work) and settles only after `implementation_slots
  .record_provider_session(...)` durably records the receipt — never waiting
  on the remote Jules task's own completion. A rejection or transport failure
  before that receipt settles immediately (nothing reusable survives); the
  slot-reservation itself is released the same way an ordinary "no capacity"
  refusal already is when admission is refused during draining.
- `issue_processor.py`'s local-implementation call and `pr_processor.py`'s
  GitHub-Actions-log repair call bind an accurate repository/target/stage for
  diagnostics; both use the default (non-deferred) settlement, since their
  produced workspace edits are already durable on disk once the call returns
  and commit/push/PR-creation are unfinished post-processing outside this
  checkpoint, not something this gate needs to wait on.

See `tests/test_invocation_admission_wiring.py` for production-boundary
regressions of this wiring, and `tests/test_invocation_admission.py` for the
standalone gate/handle model itself.

## What counts as a qualifying invocation

A qualifying invocation is a controller-initiated LLM inference/agent-run
request capable of consuming provider tokens or quota — including a
subscription-backed local CLI invocation, or the local submission of such
work to an asynchronous remote provider. Classifying a call this way never
requires a returned usage count or a monetary charge. Queueing, prompt
preparation, cache lookups, version/authentication/quota/status probes,
ordinary GitHub operations, and maintenance work are not qualifying
invocations by themselves. A running agent CLI's internal tool/model loop
belongs to the one invocation that launched it; it never authorizes a
second, independently admitted invocation. This module does not attempt to
classify calls automatically — a future caller-integration stage owns
supplying correct classifications.

## Gate lifecycle

Each daemon lifetime owns exactly one `InvocationAdmissionGate` instance,
constructed fresh at daemon startup. Its state is one of `RUNNING`,
`DRAINING`, `STOPPED`, or `FORCED`. `try_admit(repository=..., target=...,
stage=...)` is the single atomic admission boundary: under one lock, it
either registers a brand-new `InvocationHandle` (state `IN_FLIGHT`) and
returns it when the gate is `RUNNING`, or refuses and returns `None` without
registering anything when the gate is `DRAINING`, `STOPPED`, or `FORCED`.
Because `close_admission()` takes the same lock to flip the gate to
`DRAINING`, a concurrent admission attempt always either wins and is
protected, or loses and never runs its controlled provider action — there is
no admitted-but-untracked interval. A task that was scheduled before closure
but had not yet reached this boundary (still doing preparatory work) is
refused the moment it finally calls `try_admit`.

## Invocation lifecycle

An admitted invocation moves `IN_FLIGHT` → `CHECKPOINTING` →
`SETTLED`:

- `handle.begin_checkpointing(outcome)` records that the local provider call
  returned, failed, or that local submission to a remote provider completed;
  the invocation now awaits its owning caller's durable checkpoint.
- `handle.record_checkpoint_attempt_failed(error)` records a failed
  persistence attempt without changing state — the invocation stays
  protected and persistence may be retried.
- `handle.confirm_settled(confirmation_id)` / `gate.confirm_settled(id,
  confirmation_id)` settles the invocation only on an explicit confirmation
  that its checkpoint committed. It is idempotent (a duplicate confirmation
  is a safe no-op) and only ever affects the exact matching invocation — an
  unknown, mismatched, or late confirmation id never settles a different
  invocation and never grants execution authority. Settling directly from
  `IN_FLIGHT` (skipping the checkpoint step) is rejected the same way.

Nothing in the gate auto-settles an invocation on an exception, a
context-manager exit, a timeout, or a cancelled waiter; only an explicit
`confirm_settled` call ever does. `force_stop()` abandons the graceful wait
(see below) but never fabricates a settled result for an unresolved
invocation.

## Graceful readiness and draining

`gate.is_graceful_ready` is true exactly when the gate has closed admission
(`DRAINING` or `STOPPED`) and every admitted invocation has settled; it is
always false while `RUNNING` (admission has not closed) and always false
while `FORCED` (the wait was abandoned rather than completed gracefully).
Queued/unadmitted work and an already-handed-off remote task's own
completion are not part of this wait set — an asynchronous remote submission
settles on its durable local handoff confirmation, not on the remote task
finishing. `await gate.wait_until_drained()` resolves to `DrainOutcome.
GRACEFUL` once every admitted invocation has settled, or to
`DrainOutcome.FORCED` as soon as `force_stop()` is called, whichever happens
first. Any retry, continuation, correction, or provider fallback the caller
issues after closure needs its own fresh `try_admit` call and is refused
like any other post-closure admission attempt.

## Identity, isolation, and ambient scope

Each invocation's `InvocationIdentity` carries the owning gate's
`daemon_scope`, repository, target (issue/PR/repository-operation identity),
stage, and a unique `invocation_id`, so concurrent and successive calls —
including calls sharing the same repository/target/stage — never collide.
Retiring one invocation never clears a sibling that is still running or
checkpointing. Each `InvocationAdmissionGate` instance is independently
scoped: confirming an invocation id against a different gate instance (a
different daemon lifetime, or a gate that never admitted it) is always a
safe no-op. `install_invocation_gate` / `reset_invocation_gate` /
`current_invocation_gate` provide an optional ambient `ContextVar`-scoped
accessor (mirroring `shutdown_context.py`'s admission-check pattern) for
callers deep in the stack; its default is `None`, so a standalone,
non-daemon command that never installs a gate never inherits a previously
closed daemon's gate, and `asyncio.to_thread` naturally propagates the
calling context's installed gate to the worker thread without leaking it to
unrelated threads.

## Observability

`gate.snapshot()` and `gate.unsettled_snapshot()` expose the gate's state,
graceful-readiness, and each unsettled invocation's identity, lifecycle
state, and checkpoint-failure count — enough to distinguish `IN_FLIGHT`,
`CHECKPOINTING`, graceful-ready, and forced/unresolved conditions during
diagnostics. Snapshots never require or expose prompt text, response
content, credentials, or token-billing information, and never report
graceful readiness during an admission/completion race — the snapshot is
taken under the same lock that guards every state transition.

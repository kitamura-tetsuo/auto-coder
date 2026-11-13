# In-container worker supervisor

## Management CI retirement (2026-11-13)

The owner approved retiring the `management-self-update`, `fresh-host`,
`isolated-containers` and `operator-upgrade` CI jobs and their dedicated planning
and aggregate wiring. These exercise the discontinued proprietary management,
bootstrap and supervisor deployment architecture. The detailed native-profile
procedures below are historical, not active CI gates or acceptance evidence.

`Supervisor Integration Tests` retains the complete `worker-bridge` job unchanged,
including both real-process invocations, shared graceful-shutdown cases, native
result validation and artifacts. Its stable aggregate check now requires that
job to finish successfully; failure, cancellation, skipped or missing results
fail the aggregate. Ordinary PR/source/browser/live suites remain unchanged.
Retired native fixtures and their component tests remain until separate source
cleanup; no runtime implementation or safety gate is modified by this CI change.


Release and beta containers use the image-installed `python -m
auto_coder.worker_supervisor` as their long-lived PID 1. The supervisor owns at
most one `process-issues` child and accepts one newline-delimited JSON operation
per connection on the private `/runtime/supervisor/control.sock` Unix socket.
Client EOF, a transport reset, or a lost response never stops it. Supported
operations are `INSPECT`, `START`,
`REQUEST_GRACEFUL_STOP`, and `RESTART_WORKER`; mutating requests require a
1–128-character `operation_id`. Reusing that ID with a different operation,
expected identity fence, or intended runtime returns `CONFLICT` without changing
the original accepted request. An identical replay returns or reconciles only
its durable result. Requests carrying unrecognized fields (such as executable
commands, alternate users, paths, network modes or target repositories) are
rejected without starting a child.

The authenticated manager may also use the fixed `RECOVER_PREVIOUS` action,
but only for a demonstrably exited failed successor and the immediately prior
independently verified non-bootstrap READY environment. The Supervisor
persists one recovery attempt before spawning it; it never forcibly stops a
still-running child. See `in-container-worker-source-updates.md` for the
original grant, policy and restored-worker readiness checks.

## One-time deployment and startup

An operator must manually rebuild and deploy the Supervisor/bootstrap image and
the updated `compose.channels.yml` once (using `docker compose` on the host).
The containers use distinct persistent `/runtime` and `/workspace` mounts,
unchanged repository routing, and no Docker socket. The existing privileged,
host-cgroup configuration and writable cgroup mount remain available to the
worker's local-backend process owner; the lifecycle supervisor does not use
that authority or expose it through its typed operations. The supervisor cannot
install itself, update the image, or operate Docker. The authentication/network
bridge is a separate component.

On the first boot **without an existing Supervisor state file**, PID 1 attempts
one bootstrap worker start and stores its operation before spawning. When a
state file already exists, including after a deliberate stop or crash, it does
not automatically resurrect a child. Thus the startup crash-retry budget is one
attempt per new state volume, not an unbounded crash loop. An operator can
disable first-boot launch with the immutable deployment configuration
`AUTO_CODER_SUPERVISOR_AUTO_START=0` (primarily for isolated maintenance/tests).
Normal starts always use the fixed `process-issues --repo ... --log-file ...`
arguments, with the configured repository, channel, runtime/workspace roots,
management socket and a fresh generated deployment generation.

The one-time image configuration supplies the immutable instance ID, channel,
repository, runtime and workspace roots, and bootstrap interpreter. The
schema-versioned `/runtime/supervisor/state.json` is atomically replaced and
fsynced on the channel-private persistent volume. It records the selected
runtime ID, process PID and Linux start token, deployment generation, actual
worker-owned process-run ID once observed, immutable READY selection, launch
intent, effect-generation identifiers, operation payload hashes and final
receipts. A persistent `owner.lock` holds an exclusive OS lock for PID 1's
entire lifetime. A second Supervisor pointed at the same volume fails before
touching the existing control socket or spawning a competing worker; a genuine
PID-1 exit releases the lock for recovery. A malformed selected-runtime pointer,
or a missing pointer when the store records a prior worker, is treated as
`NEEDS_ATTENTION` and cannot be silently overwritten by START. Supervisor
`incarnation` changes with every PID-1 start; a persisted unresolved effect
remains fenced across that change.

## Runtime identity and inspection

Staged runtime IDs are resolved only by scanning the configured private version
root and independently revalidating exactly one immutable `READY.json` through
the worker-version verifier. Its interpreter, environment, runtime profile,
installed runtime ID, source revision, distribution version, locked-input hash
and receipt content identity must remain unchanged. Immediately before every
actual staged launch—including after a held RESTART drain—the original accepted
selection is revalidated. An absent, changed or ambiguous READY returns
`UNAVAILABLE`; there is never a fallback to bootstrap or an in-place update.

Each live `INSPECT` obtains a fresh nonce-bound management observation over the
worker Unix socket, verifies the peer process PID and Linux lifetime before and
afterward, and compares repository/channel/generation and the genuine
`process_run_id`. A staged runtime binding is `VERIFIED` only while the original
READY still independently validates and the live artifact source/version match
that exact retained lock/content identity. `CONFLICT` or `UNAVAILABLE` clears
the previous runtime binding; separately known process facts remain readable.
An image-installed bootstrap worker may expose its genuine run ID, but it never
invents a staged lock/READY identity (`binding_status=UNAVAILABLE`).

## Graceful stop, replay, and recovery

Stopping sends at most one SIGTERM via a Linux pidfd to the specific recorded
process after checking its lifetime and all supplied identity fences. The
worker's genuine `last-terminal.json` must match repository, channel,
generation, run ID and successful graceful closure, **and** the exact OS
lifetime must have exited before reporting `STOPPED`. It never sends SIGKILL,
second SIGINT or a process-group signal. Held drains remain `DRAINING` or
`UNKNOWN`; successful `RESTART_WORKER` then permits exactly one revalidated
successor. Losing a response or restarting PID 1 does not authorize replay to
repeat an ambiguous effect. An unresolved acceptance/launch or a crashed child
without terminal evidence remains `UNKNOWN`/`NEEDS_ATTENTION` and prevents
another start until deliberate operator reconciliation.

After a child PID and Linux start token have been durably associated with the
accepted start operation, a Supervisor interrupted before writing its result
can reconcile that same operation. It requires the exact live OS lifetime and
a fresh peer-verified worker observation with the accepted deployment
generation. An absent observation stays in progress; a missing or mismatched
lifetime never authorizes another launch. This records a process start, not a
verified staged runtime or healthy worker.

The narrow `RECOVER_PREVIOUS` exception can replace the exact confirmed-exited
failed child without a graceful receipt; ordinary `START` remains blocked.

When Docker sends PID 1 SIGTERM for a manual container stop, the Supervisor
requests the same cooperative drain and waits for the child to exit; it does not
force the worker to meet an observation timeout. Host-side container stop
policies still determine the ultimate shutdown deadline, outside this process's
authority. Supervisor logs go to colored-capable stdout and to rotating
`/runtime/supervisor/supervisor.log` without logging arbitrary request payloads.

## Isolated CI integration

The `Supervisor Integration Tests` GitHub Actions workflow retains the independent
**worker-bridge and worker-daemon** pytest invocations on one worker runner. Three
additional, independently planned profiles—`management-self-update`, `fresh-host`,
and `isolated-containers`—each collect their *complete current* selection before
dispatching a dynamic matrix of at most 12 nonempty, contiguous time-budgeted
shards. Each matrix uses `max-parallel: 4` (at most 12 long-profile execution
jobs concurrently across the three matrices, plus the worker job); each shard
gets its own ephemeral runner and repeats the real profile prerequisites. The
stable required check is still named `Supervisor Integration Tests`, always runs,
and now refuses unless the native workers, all three plan jobs, all dispatched
matrix jobs and every native per-shard pytest receipt are complete and successful.

The shared source of profile selectors and stable anchors is
`scripts/supervisor_ci_profiles.py`, consumed identically by planning and
execution. The unchanged worker/daemon selectors, whole-file selections in all
three long profiles, the five special fresh-host node selectors, and all six
parameterized installed-handoff nodes are preserved. The complete fresh-host
required-node registry includes the four archived-cutover variants and six
installed-handoff variants; the isolated-container registry contains the four
production Supervisor, one worker-network, one beta-deploy and six smoke nodes.
Removing a required marker fails **full collection before the matrix launches**.
Each result-inventory plugin checks only its own shard's *intersection* with the
full required-anchor registry, and the aggregate compares every exact outcome
and the union of all assignments against the immutable full profile inventory;
node IDs repeated intentionally in management and privileged profiles are never
de-duplicated globally.

Every job uses Python 3.12, uv 0.7.22 and locked test extras. Both preparation
and execution perform the checked-out image build for host/container profiles;
fresh-host additionally tags the production image, pulls the pinned Caddy image,
installs `strace` and runs the original `-o timeout=240` selectors under root
with the checked-out venv/PYTHONPATH. The container profile retains the
container-preparation cache and `AUTO_CODER_REQUIRE_SUPERVISOR_INTEGRATION=1`.
Native pytest cancellation, fixture assertions, child/process/HTTP/SQLite/Git
oracles and test deadlines remain unchanged. Exactly four Docker-proxy
management duplicates may keep their original `host root is required to observe
the disposable Docker proxy` skip reason; the fifth duplicate may keep the
original `production-profile image is not available locally` skip reason.
Those exceptions are **management-only**: a missing required privileged or
container verdict is never replaced by a management skip.

Every full-profile plan binds the actual tested `git rev-parse HEAD`, selectors,
effective Python/locked dependencies, tracked harness and semantic environment.
For pull requests this is the synthetic **merge** commit, not the PR-head SHA
returned by the GitHub workflow API. The checked-out source-bound image tag and
per-shard timing/report-control paths are derived from the source or plan, not
independent configuration identities. Planning uses
`SUPERVISOR_CI_RESULT_MODE=collect`, which cannot produce executed PASS receipts;
execution uses both `scripts.ci_shard_plugin` and `supervisor_ci_results` with
identical semantic pytest selectors. A plan artifact is immutable and scoped to
the workflow run, producing planning attempt and profile.

Each native shard uploads its full JSON result inventory, JUnit diagnostics,
native timing JSONL and a separately bound Actions receipt. A receipt binds
actual checkout, event SHA, run, producing attempt, logical profile/shard,
immutable plan digest, numeric producing job ID, runner identity and native
inventory hashes. The required aggregate reads *all* Actions job pages and
attempts through the existing read-only GitHub boundary (`actions: read` and
`contents: read`; no new write authority). On GitHub `rerun-failed-jobs`, an
original successful job may appear in the current attempt with a **new alias
job ID**. Reuse is allowed only when the complete native step results,
execution timestamps, logical matrix name and runner identity uniquely identify
the original producing job, and its matching original receipt is still present.
Missing, ambiguous, foreign, new-plan or stale-source receipts fail the gate;
rerunning the entire workflow is required when retained attribution cannot be
proved. A green matrix or plan artifact alone has no result authority.

An optional update after the successful aggregate materializes one
`ci-duration-profile-v1` snapshot **per long profile** from original native
timing JSONL plus explicit per-shard batch-origin manifests; JUnit is kept only
as outcome diagnostics. Snapshots are uploaded under run/attempt/profile-qualified
names, restored from previous run artifacts when available and used only for
conservative estimates. Failure to restore/update timing is diagnostic, while
loss of a required plan, case inventory, job or native receipt fails CI. The
aggregate uploads queue, runner setup, test-phase duration, overlap, wave peak,
total runner work, capacity-limited and indivisible over-budget diagnostics.
With no matching native duration history, the Supervisor workflow now supplies
a **120-second cold-start estimate** for each unknown test node. This is an
explicit scheduling heuristic, not an invented native measurement: the generic
B planner retains its default one-second fallback for other callers. The
conservative bootstrap prevents all expensive fresh-host cases from being
placed on one runner merely because history has not yet been produced. Real
native P95 measurements take precedence when greater, while still-unmeasured
nodes retain at least the cold-start estimate. An indivisible case may exceed
420 seconds; the twelve-shard cap may raise the effective grouping budget.
Later successful runs can use native duration snapshots. The 420-second
estimated work budget and four parallel runners do not guarantee a ten-minute
end-to-end SLA, available capacity or a particular Actions queue time.

The container group runs network-disabled,
capability-dropped disposable Docker containers. Each worker service's fixed
Compose entrypoint makes the Supervisor PID 1 with no accessible Docker socket/CLI,
while the image's default `auto-coder` entrypoint remains available to management
and other non-worker services. Separate release/beta
volumes run a fixed harmless test child backed by the real management bridge:
the job holds a true SIGTERM drain until that bridge writes a matching terminal
receipt, rejects changed-payload replay, proves the peer channel remains live,
checks a single successor on replayed RESTART, and reconstructs persisted state
after killing the isolated Supervisor container. A separate real-process test
keeps the management-bridge child in held drain after dropping the original
stop response and killing its Supervisor; a reconstructed Supervisor must
replay only that accepted stop. The suite also checks duplicate Supervisor
ownership and corrupt pointer recovery. It never mounts production
state, supplies GitHub tokens, invokes an LLM provider or runs the issue/PR
scheduler against a real repository.

The dedicated entrypoint check runs the unchanged production worker Compose
command (`python -m auto_coder.worker_supervisor`) as PID 1. The live-CLI
acceptance test uses the same production Supervisor with a fixed test launcher
that execs the real `process-issues` CLI in its selected Python interpreter.
It passes supported CLI flags to bypass only the external GitHub credential
preflight and optional secret sync; no application implementation is mocked.
A disposable
installer container first creates a real #2610 READY environment through Git
checkout, frozen `uv` installation, embedded source provenance and independent
READY verification. Only the installer can download dependencies. The actual
worker container has no network, usable GitHub/provider credentials, Docker
socket or Docker CLI; it uses a fictitious private repository/ownership volume.
The test verifies the real bootstrap `process-issues` UDS lifetime and graceful
exit, then a positive staged `process-issues` start using the READY-selected
interpreter and a fresh worker-owned, nonce-bound UDS observation. Replacing
READY with a different independently valid runtime identity at the same source
SHA, changing locked inputs, or replacing the child lifetime must invalidate
the original binding. Two independently staged READY environments also exercise
a held SIGTERM restart through the production typed Supervisor endpoint: lost
response, same-ID changed intended READY/fences and replay must preserve the
original accepted destination and start only one successor. Reconstruction and
peer noninterference are separately exercised against the actual worker
management bridge. No real GitHub issue is dispatched.

The normal PR test shards exclude the `supervisor_integration` marker. To
reproduce a complete profile locally, run
`python scripts/supervisor_ci_profiles.py args --profile <profile>` to obtain
its complete selectors, configure its full anchor registry and approved skips
with the `configure` command, and follow the prepare/collect steps in
`.github/workflows/supervisor-integration-tests.yml`. Use
`scripts/plan_ci_shards.py --context ... --output ... --log-dir ... -- <exact
profile args>` (source and Python must match), then run
`pytest -p scripts.ci_shard_plugin --ci-plan=... --ci-shard=N <same exact
profile args>` with `SUPERVISOR_PLAN_PATH`, `SUPERVISOR_SHARD_ID`,
`SUPERVISOR_PROFILE` and `SUPERVISOR_RESULTS_PATH`. Do not replace whole-file
selectors with fixed node lists or de-privilege the host profile. Reproduce the
container profile with the checked-out production image,
`AUTO_CODER_REQUIRE_SUPERVISOR_INTEGRATION=1` and the separate nested UV cache.
The root fresh-host profile additionally requires Docker, pinned Caddy, `strace`
and the checked-out Python interpreter. Inspect the original producing
run/attempt/shard artifact and the final aggregate `native-metrics.json`, not
merely GitHub's current-attempt alias or runner setup timestamps. Actual test
interval overlap must be observed in Actions before claiming acceleration;
queue and image preparation costs remain outside the estimated test budget.

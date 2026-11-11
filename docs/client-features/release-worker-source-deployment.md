# Authenticated release worker source deployment (isolated-host acceptance)

The `auto_coder_deploy_release_source` management MCP action is implemented as
an explicit, instance-bound request using the existing OAuth read+deploy grant,
the operator-owned `full` release policy, the shared per-instance operation
journal, immutable version preparer and signed Supervisor control lane. It is
not an automatic consequence of promotion or an OCI/Docker deployment.
**PR #2832 remains draft until its exact-head host tests, CI and review gates
are complete.** Isolated-host acceptance of disposable installed workers is
not authorization to deploy, restart or replace the live release service.

## Trust boundaries and request

The operator configures the release worker target, fixed repository, private
Supervisor endpoint and secret, promotion workflow ID, source-release policy
and GitHub credential file. The MCP caller cannot select any of those, or a
command, shell, runtime path, image, Docker container or executable. The
target must have channel `release`, match the promotion repository and remain
permitted for `ActionClass.DEPLOYMENT` by the current `full` policy. The
original accepted OAuth grant must still include management read and deploy
scopes before each newly dispatched worker effect.

Supply all these parameters to the authenticated MCP tool:

| Argument | Required identity |
| --- | --- |
| `target_id` | Operator-configured release worker instance alias |
| `selected_git_sha` | Exact lowercase 40-hex Git commit |
| `promotion_operation_id` | Original successful accepted-source promotion management operation |
| `catalog_ref`, `catalog_oid` | Immutable `auto-coder-source-release-<operation_id>` annotated catalog ref and object OID |
| `acceptance_ref`, `acceptance_oid` | Original exact published source-acceptance ref/object |
| `idempotency_key` | Stable key; reuse unchanged after lost replies |
| `expected_supervisor_generation` | Current signed Supervisor incarnation |
| `expected_process_run_id`, `expected_deployment_generation` | Current running child's signed OS-lifetime identity, or both omitted when stopped |
| `expected_runtime_id` | Current selected installed runtime identity |

The read-only `ReleaseWorkerEvidenceReader` joins the original succeeded
management promotion, its immutable journal intent/admission, bound successful
Actions attempt, published acceptance, tested source record, schema-2 catalog,
GitHub Release and source-channel current selection. Independent observations
are repeated before acceptance. Expired evidence, stale or missing refs, a
different repository, changed policy or original operation, and a later
promotion of **the same SHA** fail closed. This reader by itself never grants
deployment permission or dispatches worker effects.

The release source commit ref
`refs/tags/auto-coder-release-source-current` is paired with
`refs/tags/auto-coder-release-source-selection-current`, whose target is the
annotated catalog object identifying the selecting promotion operation. Every
promotion advances the operation-selection ref after confirming the direct
source commit ref. The release writer must remain serialized through the
existing `release-channel` Actions concurrency lane. GitHub does not offer
an atomic transaction across those refs, GitHub Releases and the journal:
ambiguous writes, unrelated external writers and incomplete postconditions
require operator investigation, never optimistic replay.

## Lifecycle and readback

1. Complete the separately authorized accepted-source promotion. Verify its
   `SUCCEEDED` management operation, immutable catalog and original acceptance
   OIDs. Record the release worker's signed Supervisor, process and selected
   runtime identities.
2. Invoke `auto_coder_deploy_release_source` with the original facts and a
   stable idempotency key. Identical same-key requests return the original
   operation; changed-intent reuse returns `CONFLICT`. A competing key for the
   active release instance returns `BUSY`, even if source selection changes.
3. Poll `auto_coder_operation_get` with the exact `target_id` and returned
   `operation_id`. `PREPARING` stages the candidate before any drain. Exact
   accepted SHA, lock digest and version must equal the immutable preparer's
   durable `READY` artifact. The exact selected promotion and original
   authority are checked again immediately before the **first** replacement.
4. `DRAINING` requires the original Supervisor's correlated terminal graceful
   receipt and independent observation of the incumbent's real OS exit.
   `STARTING` verifies the old lifetime is gone and selects only the
   independently installed runtime ID. No force kill, container restart,
   host reboot or peer beta worker mutation is authorized.
5. `RECONCILING` accepts only the genuine new process run and generation,
   verified runtime binding, build-embedded source SHA, installed distribution
   metadata, startup reconciliation, serving daemon, running invocation gate
   and at least two strictly advancing producer-origin heartbeat sequences.
   A launch receipt alone never establishes `SUCCEEDED`.

Each possibly sent step persists the original journal attempt, owner
generation, correlation, fixed Supervisor operation ID and digest **before**
dispatch. Lost replies are reconciled by the original durable receipt.
Current policy/grant loss, missing selection, ambiguous process exit and
unresolved original operations retain the same journal lane; no timeout
permits replacing them with a different request.

If the new child exits after activation, recovery may start **at most one**
previously verified prepared runtime, with a separately persisted step,
after actual new-child exit and with current authority. A release-only candidate
which is still alive but has not achieved required startup/producer readiness by
its bounded reconciliation deadline has a second, explicitly destructive fallback:
the Supervisor durably requests `RECOVER_PREVIOUS_REBOOT` for that exact failed
generation and prior READY, then exits **its own container PID 1**. Docker's
already configured `restart: unless-stopped` restarts the **same container**.
The new Supervisor consumes the one-shot durable intent and launches the prior
verified runtime before reporting any recovery success. Management invokes only
the authenticated, signed Supervisor operation; neither management nor the
Supervisor opens Docker's socket, executes `docker`, or creates/replaces a
container. The readback exposes `recovery_method` (`EXITED` or `REBOOT`),
`recovery_attempts`, `recovery_state` (`NOT_ATTEMPTED`, `ATTEMPTED`,
`UNKNOWN` for an unsettled step, `CONFIRMED`) and `restored_worker`.
Confirmed recovery ends new-source
deployment as `FAILED`, not `SUCCEEDED`. The release readback retains
`new_worker_failed_previous_runtime_restored` while existing beta readback
retains `new_beta_worker_failed_previous_runtime_restored`, so release
admission does not silently change beta failure diagnostics. Unknown recovery
is not automatically retried. The forced path discards all candidate process
state and unfinished coding-agent work; external GitHub/provider effects already
performed are not undone. It requires a running Docker daemon and the existing
restart policy and cannot guarantee recovery if those or the prior READY are
unavailable. Restoring Python cannot undo migrations, database
state, provider effects or publication; those require explicit operator
rollback/bootstrap investigation.

## Verification boundary

`tests/test_release_worker_deployment.py` exercises original promotion
and acceptance, authenticated MCP admission, same-SHA supersession,
replay/CONFLICT/BUSY, policy changes, lost stop receipts, prior-runtime
recovery and rejection of worker artifact fields with a controlled
Supervisor bridge. `tests/test_management_worker_lifecycle.py` and
`tests/test_worker_supervisor_bridge.py` separately exercise actual
subprocess, UDS, HTTP, held drain, OS exit and Supervisor reconstruction.
These controlled tests are **not** a substitute for the integrated
production-origin release suite described below. Record exact original
positive-control evidence, host/image revision, CI results, review disposition
and operator acceptance against Issue #2613 before marking the PR ready.

A bounded regression command is:

```bash
uv run pytest tests/test_release_worker_deployment.py tests/test_management_source_release_dispatch.py tests/test_source_release_promotion.py
```

There is no new dashboard scheduler/provider event, Docker socket or runtime
host authority. Existing management Loguru sinks record admission and
reconciliation. Related observability is in `docs/dashboard-observability.md`.

### Native release bootstrap reconstruction regression

The authenticated bootstrap START and RESTART lost-response regression also runs
with a release target under the full operator policy. It crosses the real signed
HTTP bridge, Supervisor, worker Unix socket and OS child boundary, then reconstructs
the manager and Supervisor and verifies that the original operation and exact
child lifetime are retained without a second START dispatch. A bootstrap-only
child still cannot report SUCCEEDED: this is not installed accepted-source
release activation evidence and does not replace the remaining integrated
REQ-009/010/011 acceptance gates.

The native release RESTART case also holds the actual old OS process after its
genuine matching terminal receipt is durable. While it is alive, repeated
management reconciliation must not dispatch START, replay retains the operation,
and a competing key remains BUSY. Only releasing the observable exit barrier
permits the original replacement to proceed. This covers the terminal-versus-exit
boundary without synthesizing a successful drain receipt.

### Reconstructed Supervisor after a retained source effect

Release source deployment can adopt a reconstructed Supervisor incarnation only by
reading the original persisted drain or start operation ID and exact intent
digest. A completed drain additionally requires the old selected runtime still
selected and an independently observed STOPPED state with no live child. A start
requires its exact accepted child generation/run and a fresh VERIFIED installed
binding matching the original staged runtime ID, content identity, source SHA,
distribution version and lock digest. Adoption only updates the current owner
observation; it does not replace the original effect identity or dispatch again.
Each later mutation still passes original grant/policy and source-selection gates.

Missing/mismatched receipts, a live old child, a different installed binding or
an unconfirmed observation retain the original operation/lane without a new
transition. A retained recovery uses only its original `RECOVER_PREVIOUS` or
`RECOVER_PREVIOUS_REBOOT` effect
ID and intent digest, which binds the original failed-child fence and selected
prior runtime. It additionally requires exactly one recorded recovery attempt,
the original verified prior binding, a distinct restored child matching the
receipt, the same freshly observed Supervisor incarnation and the prior runtime's
verified source/content/version/lock identity with production artifact origins.
The failed candidate or original incumbent cannot be adopted as the restored
child. An earlier candidate START receipt never substitutes for recovery evidence.
Only the current owner observation changes; operation, authority, attempted-effect
identity and deadlines remain original, and adoption dispatches nothing.

Unavailable recovery receipt/inspection/binding observations wait within the
original deadline. Conflicting evidence retains `NEEDS_ATTENTION` and the busy
lane. Once original correlated evidence is observable, the normal prior-runtime
readiness/progress checks can finish the original deployment as `FAILED` with
recovery `CONFIRMED`. This readback does not create new failure authority.
An explicit startup-abandonment fact may reserve the graceful live-candidate
recovery below. Separately, for release alone, the exact PID-verified candidate
that remains unready at its original bounded reconciliation deadline may use
one destructive Docker-managed PID1 exit. Neither path bypasses the prior
incumbent's genuine terminal-drain-and-exit requirement, resets an existing
recovery's deadline or acquires a second recovery budget.
Preparation-only reconstruction is not admitted by this rule. Controlled
signed-boundary regression cases cover recovery reconstruction and its negative
cases. The installed recovery reconstruction cases below exercise the original
receipt and genuine worker join; their exact clean-revision runs remain required.
If expiry has already moved a release recovery to `NEEDS_ATTENTION`, a separate
recovery-only read path can still verify that original effect. It never resumes
a dispatch-capable stage or changes either deadline. It requires the original
actor's current grant/policy, the same exact receipt/prior-runtime evidence and
two newly observed, strictly advancing producer heartbeats from one restored
child, together with startup reconciliation and daemon serving. Candidate or
pre-expiry samples cannot count. Missing/mismatched evidence, nonadvancing or
diagnostic-only activity and original authority loss retain the lane; a new login
does not substitute for a revoked original grant. Only after the full proof is
durable does the existing evidence-bearing journal resolution finish the same
operation as `FAILED` with recovery `CONFIRMED`. Failed snapshot or terminal-row
persistence does not release the lane. The historical attention category remains
in the journal. Readback uses the final recovery reason only after a matching
terminal row, lifecycle snapshot and same-correlation lifecycle receipt are all
durable; a snapshot or unrelated receipt cannot replace that historical reason.
Other uncertain lifecycle effects are not made resumable by this narrow path.

Transient live-binding unavailability waits within the original lifecycle deadline,
without dispatch or lane release; fresh matching evidence can then continue the
same operation. Expiry retains NEEDS_ATTENTION until the recovery-only proof
above is available; other uncertain steps remain held. Beta source reconstruction
is unchanged by this release-specific extension.

### Installed acceptance join regression

`tests/test_release_worker_acceptance_container.py` joins authenticated beta
acceptance and source promotion to authenticated `auto_coder_deploy_release_source`
admission against a separately running release worker. The external fixture
independently installs the prior and candidate full Git revisions with the
existing locked version preparer. Actual beta process observations produce
`PASS_PUBLISHED`; the acceptance and release catalog are real annotated Git
objects in a disposable repository. The authenticated deployment retains those
exact identities while the signed Supervisor drains the prior installed worker
and starts the candidate. Independent signed samples check installed provenance,
startup reconciliation, daemon serving and producer heartbeat advancement.
Actual process launches, container identities, beta identity, ownership files
and source refs are observed separately from management success readback.

The external Actions/Release transport is controlled and executes the production
promotion publisher against real Git; it is not a live GitHub Actions acceptance
claim. Neither worker has a Docker binary or socket. The test belongs to the
required `isolated-containers` Supervisor profile rather than ordinary unit CI.

The failed-candidate case uses the same genuine acceptance and installed-runtime
join. Only after the prior installed worker is serving, an external one-time
fixture fault makes the actual candidate interpreter exit before daemon readiness.
Independent signed observations and OS lifetime records identify that failed
candidate; the original Supervisor starts the exact prior prepared environment
once. The case checks its installed provenance and advancing producer heartbeat,
three actual launches (prior, failed candidate, restored prior), the original
operation/authority on repeated replay and terminal `FAILED` with recovery
`CONFIRMED`. The beta peer, source refs, containers and ownership remain unchanged.
This proves recovery of executable code, not rollback of persistent data or schema.

The lost-START-response case runs authenticated deployment in a separate manager
OS process. Its external transport fault discards the actual verified signed
START response and subsequent reads of only that original operation. An
independent signed client still observes the installed candidate and retained
Supervisor receipt. The fixture kills the first manager and starts another Python
process with a fresh client against the original OAuth database, UID policy and
operation journal. While the response is unavailable, original replay remains
`STARTING`, changed source/catalog intent conflicts and another key remains busy.
The effect ID, owner, attempt, correlation, digest, deadlines, accepted evidence
and authority remain original, with no further signed POST or worker launch.
Releasing only transport loss lets the original signed receipt and independently
observed producer progress settle the same deployment successfully. Actual PID
and start-token checks establish that the candidate survived the manager restart.
Requests enter the child process as HTTP documents over a fixture pipe and pass
through the real authenticated ASGI app; this is process/state reconstruction,
not an external TLS listener restart. No accepted authority or runtime evidence
is synthesized.

A separate reconstruction case discards a real signed START response, closes the
management app and reconstructs its app/client objects against the original
journal. An out-of-band fixture signal unwinds the Supervisor listener and creates
a new Supervisor object in the same PID1 process, retaining the installed child.
The new signed Supervisor incarnation must reconcile the original unknown START
without another replacement launch. This is app/Supervisor object and listener
reconstruction, not a manager or Supervisor OS-process restart. It complements,
and does not replace, the separate manager-process case above. The same case
first refuses the durable drain-boundary journal write and checks that only the
already completed PREPARE was posted while the original incumbent remains ready.

The post-stage selection case holds the manager immediately before its drain
step, publishes a later promotion for the same Git SHA through the authenticated
promotion tool, then resumes. The original deployment must become `SUPERSEDED`
without any drain/start POST or replacement launch. Same-source identity alone
cannot substitute for the originally accepted promotion/catalog identity.
A separate stopped-incumbent variant first obtains the prior installed worker's
real graceful terminal receipt and independently verifies its exact OS lifetime
is absent before admission. With no unresolved stop, it stages the candidate and
holds the first START boundary. A second same-SHA promotion must supersede the
original before any runtime-pointer/start effect. Only PREPARE may follow the verified signed initial STOP, the selected prior
runtime stays stopped, and no new interpreter launches. The deployment must preserve all refs produced by the deliberate
second promotion, both original immutable evidence objects, beta and ownership.

The terminal-exit case runs the installed prior worker's actual CLI, wraps only
its terminal-persistence return, and holds that process after the production
bridge writes its genuine graceful terminal receipt. While its OS lifetime is
still alive, the manager must not dispatch START, replay retains the operation,
and another key remains busy. Removing the external hold permits genuine process
exit and the original candidate activation. No terminal or readiness data is
synthesized by this fault.
Its reconstruction variant holds the same actual prior PID/start token and
terminal receipt while killing/recreating the separate manager OS process and
reconstructing the Supervisor object/listener in the same PID1 process. The
original drain receipt must still be DRAINING while that OS lifetime exists.
The manager retains its original step, authority, accepted evidence and deadlines;
replay and conflict/busy exclusions cannot free the lane or issue another POST.
Only removal of the external hold and real old-process exit may allow the
original candidate START. No Supervisor OS-process restart is claimed.

The uncertain-recovery case discards a real `RECOVER_PREVIOUS` reply after the
candidate actually exits, withholds that operation's receipt and reconstructs
manager app/client objects against the original journal. While the exact prior
installed worker resumes, the original operation and lane remain owned, with
no further recovery or child launch. Revealing the original receipt confirms
recovery as `FAILED`, never `SUCCEEDED`. This case does not restart the manager
OS process or change the Supervisor incarnation.

Two additional cases reuse that same actual failed candidate and original
recovery. They discard the signed `RECOVER_PREVIOUS` response only after the
production client verifies it, then discard readback of exactly that operation.
The first manager OS process is killed and a fresh Python manager starts against
its original journal, OAuth database and UID policy. The Supervisor object and
signed listener are reconstructed separately in their existing PID1 process;
this changes the signed incarnation without killing the retained installed
worker. Independent signed receipts, `/proc` PID/start tokens and launch audits
must show the same restored prior child and exactly three total launches.
While readback is lost, original replay/authority/deadlines/effect identities
remain unchanged, altered source/catalog conflicts, another key stays busy,
and neither new signed POSTs nor another recovery are permitted.

The post-deadline variant waits out the original production 150-second recovery
window without editing time or the journal. It requires `NEEDS_ATTENTION` with
an unknown effect and an owned lane before releasing only the transport fault.
The original receipt and two new genuine producer heartbeat samples must then
settle the same operation as `FAILED` with recovery `CONFIRMED`, using the
original expired deadline and a correlated durable terminal journal receipt.
The normal variant releases readback before expiry. Both require independent
installed provenance/progress and unchanged beta/ownership/source identities.
These are fixture-pipe authenticated ASGI requests and Supervisor object/listener
reconstruction, not a Supervisor OS-process restart or external TLS test.

Another app-only variant changes the Supervisor incarnation while retaining
the original unknown recovery receipt. It uses a fresh manager app/client,
not a separate manager OS process.

Another installed acceptance case starts with a failed candidate that genuinely recovers the prior
installed worker, then attempts another authenticated deployment while the
restored prior's actual terminal receipt is held ahead of OS exit. After
manager-app and Supervisor reconstruction, the original running lifetime must
remain fenced until that OS exit is observed. This test fabricates no
terminal receipt or prior child identity. It is a second
deployment after the first has already finished FAILED with recovery CONFIRMED;
it is not a held child during the original recovery direction.

All integrated cases verify original immutable replay, changed-intent
`CONFLICT`, competing-key `BUSY`, untouched beta process/container identities,
routing ownership and exact OS launch counts. Successful and recovered-runtime
cases additionally verify independent installed provenance, producer heartbeats
and unchanged release Git refs; the supersession case intentionally advances
only the source selection. Focused component tests additionally
deny further effects after the original OAuth grant expires or is revoked and
after the operator policy is downgraded. The fixture uses no Docker socket or
binary inside the exercising runtime; the outer test fixture alone creates,
inspects and removes disposable Docker containers. The forced release recovery
case also verifies Docker's configured restart policy relaunches the same
container after PID 1 exits and validates the restored READY, receipt and
producer heartbeats. The source promotion's external Actions/Release
HTTP remains controlled, so this is not a live GitHub publication or a live
release rollout.

Do not remove the draft/review acceptance gate solely because individual tests
passed on a different head. Record a clean checked-out commit/image, complete
the relevant bounded host tests, verify live release/beta service identities
were unchanged, then require that commit's CI and review disposition. An
unknown recovery, unverified installed binding or incomplete original receipt
must never be promoted to `SUCCEEDED` merely to clear the release lane.

These descriptions and required selectors are not passing acceptance evidence.
Acceptance remains gated on the relevant exact clean-revision host results, CI
and review disposition, including the original live failed-startup recovery
transition below.

The graceful installed release cases reconstruct a fresh Supervisor owner/listener and
signed incarnation from persisted state while retaining the actual worker in
the same PID1 process. They do not establish survival across a Supervisor
OS-process crash. The separate component case
`test_release_beta_held_drain_replay_restart_and_crash_reconstruction` exercises
an actual beta PID1/container crash, reconstructs the persisted worker identity as
`UNKNOWN` and refuses another START. Killing the current PID1 destroys its
worker PID namespace, so a retained live child must not be inferred at that
boundary. The distinct destructive release fallback now has its own required
installed test: it must prove actual PID1 exit, one autonomous Docker restart
of the same container and original prior-runtime recovery. It does not claim
that the failed candidate survives the namespace loss. Its exact clean-revision
host and CI results must be recorded separately from the graceful/object cases.

Held invocation/checkpoint ownership is separately exercised through the actual
daemon and UDS in
`test_actual_release_daemon_abandons_startup_but_owns_checkpoint_until_graceful_terminal`.
It admits a real invocation-gate handle, holds an unconfirmed checkpoint and
requires ordinary drain to wait until that original handle settles before the
worker emits its terminal receipt. The installed release join independently
holds the genuine terminal receipt ahead of exact OS exit. These are
complementary producer and integrated transition checks; they do not claim the
full combination of active invocation, every reconstruction fault and installed
release recovery in one scenario.

### Live failed-startup recovery

An explicitly abandoned startup lifetime can now enter `RECOVERY_DRAINING` only
when its signed, typed failure fact belongs to the exact installed candidate and
the original deployment retained a verified prior runtime. The one recovery
budget is durably reserved before any recovery effect. A distinct original
`recovery_drain` step (`f-<operation_id>`) requests ordinary graceful shutdown of
that failed child, using its original run, generation and runtime fence. Both
this step and the later `RECOVER_PREVIOUS` launch independently require the
original actor's current grant and operator policy. Neither uses force.

Supervisor captures the prior verified READY provenance on candidate activation,
before the candidate's later graceful terminal can replace `last_worker`. Recovery
of that stopped candidate requires its retained typed failure, exact original
terminal, actual OS absence, unchanged selected candidate and the preserved prior
binding/READY. The failed generation can consume that recovery only once.
Reconstruction reads the original recovery-drain receipt and digest; it never
substitutes the earlier incumbent drain or candidate START. A held OS lifetime
keeps the receipt `DRAINING` and the original lane owned. Only the matching
`STOPPED` receipt plus independent absence permits the separately journaled prior
launch. The restored prior must still pass its normal installed readiness and
advancing producer-heartbeat checks, and the deployment ends `FAILED`, recovery
`CONFIRMED`.

If the recovery drain expires before a prior launch was dispatched, the operation
remains `NEEDS_ATTENTION` with the lane owned while evidence is inconclusive.
Later exact original drain receipt/digest, unchanged failed runtime and genuine
OS absence can resolve it using reads, including after Supervisor reconstruction.
All other possibly sent steps must already be confirmed, the original authority
must remain valid, and the correlated terminal snapshot and journal receipt must
be durable before lane release. The original operation then ends `FAILED`,
recovery `NOT_LAUNCHED`, reason `failed_candidate_stopped_recovery_not_launched`.
The operator must inspect the stopped instance and explicitly choose any later
recovery. No deadline is renewed and no prior launch is dispatched. The separate
postdeadline restored-runtime settlement above applies only to an already-sent
`RECOVER_PREVIOUS` effect.

The required installed case
`test_live_failed_installed_candidate_drains_across_reconstruction_before_prior_recovery`
uses one external startup-attempt fault; the installed engine itself decides
abandonment and emits the typed fact. It holds the real failed child's OS lifetime
only after its real graceful terminal is durable, reconstructs the manager app
and Supervisor object/listener, and requires unchanged original ownership, no
extra dispatch/launch, exact old OS exit and one independently installed prior
recovery. This is app/object reconstruction in the same test/PID1 processes, not
an OS Supervisor restart. The new case requires a clean exact-source image/run;
its presence in the required CI selector is not a passing acceptance claim.

The companion required installed case
`test_expired_live_failed_installed_drain_settles_without_prior_launch` holds that
same genuine terminal/live OS lifetime through its unchanged production
300-second recovery-drain window. It then allows actual exit and requires exact
original read-only correlation, durable `FAILED`/`NOT_LAUNCHED`, no unknown step,
no third worker launch, no recovery dispatch and unchanged deadlines. It verifies
original journal authority/receipt, replay without effects, beta peer identity,
container/ownership/source refs and the retained failed OS identity independently.
It also requires its own clean exact-image host result before acceptance is claimed.
The destructive release fallback deliberately does not preserve the candidate's
active invocations or OS process lifetime. Other held-incumbent-drain and
unknown-effect scenarios still require their original non-destructive evidence.
Passing a disposable Docker restart test is not a claim that the live release
host was restarted: preserve separate deployment approval and clean-revision
CI/review gates.


The destructive fallback's durable intent also pins the original PID1 OS start
token. A fresh Python object or signed incarnation is insufficient: consumption
requires a positively read, different PID1 start token, the failed candidate's
absence, the original request digest and pinned prior READY. An equal, missing or
unreadable token refuses launch and preserves the original intent for attention.
The installed reboot acceptance waits the unchanged 150-second candidate window,
checks no effect before expiry, and verifies independent signed prior producer
advancement as well as the manager's actual signed heartbeat inputs. It preserves
publication refs, routing ownership and the beta peer, and proves exactly one
same-container restart with no duplicate effect on replay. These assertions are
required gates, not a substitute for recording an exact clean-revision test run.

Destructive eligibility requires the original deployment's correlated confirmed
`d-<operation_id>` STOPPED receipt and digest, not merely a usable previous READY.
An originally stopped instance cannot use this exception. The manager also
requires a fresh signed exact installed-candidate observation affirmatively
showing startup incomplete and daemon not serving, both before reservation and
before dispatch. Missing or unavailable observations, incomplete fields, a ready
candidate or a held drain refuse the effect and preserve the owned lane.
Supervisor independently checks the prior terminal/OS exit and the exact local
candidate observation at request time and again immediately before its own exit.
A changed or unavailable observation blocks that original intent; storage failure
cannot escape as an exception-driven PID1 exit. The final observed lifetime is
durable before exit and must match the original intent when the new PID1 consumes
it. This is a fresh local check, not an atomic lock on the worker's future startup
progress. The disposable acceptance holds actual startup reconciliation while
the installed worker's original UDS and readiness producer remain available.

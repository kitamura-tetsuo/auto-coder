# Management recovery epoch integration design

Issue [#2824](https://github.com/kitamura-tetsuo/auto-coder/issues/2824) owns a
new host ownership epoch after continuous exclusion was lost. Its required
result is a current, exact-target, one-use grant composed from independently
verified original history, current containment and retained fences. This
fragment records implementation boundaries found at source
[`e742338b3f311e6d30a22a8f5341b8072513d813`](https://github.com/kitamura-tetsuo/auto-coder/commit/e742338b3f311e6d30a22a8f5341b8072513d813).
It is a design and prerequisite audit, not a recovery procedure or evidence
that a production grant can currently be issued.

The bounded durable-state prototype accompanying this design does not wire a
production issuer or authorize recovery. In particular, an unused state API,
component test, operator inventory or saved prerequisite verdict cannot satisfy
the full Issue. Existing unknown-outcome refusals remain correct. No original
claim, missing journal, operation key, target root or initialization outcome is
repaired or reset by this design.

## Existing authoritative producers

The prerequisite implementations have useful, distinct contracts:

| Producer and verifier | Existing fact | Limit relevant to the recovery issuer |
| --- | --- | --- |
| `original_attempts.enroll`, `capture_claim`, `grant_boundary`, `read_frontier` | Protected prospective attempt anchors and a serialized, append-only history frontier. | Enrollment starts a new prospective scope. It does not capture the origin of a previously running baseline deployment. |
| `original_attempt_evidence.derive` and `verify` | Exact original attempt/store bytes and identities, reverse successor enumeration within the recorded scope, current fences, original terminal never-created facts. | The first anchor's old instance is an external baseline. The verifier also refuses original outcomes other than `succeeded`; it does not expose a complete-but-unknown recovery evidence contract. |
| `quarantine_history.load_manifest` and `QuarantineHistory` | Read-only inspection of preserved operation journals and their key/target exclusions. | An inspection manifest is a selection, not complete lineage authority. |
| `containment_observation.observe` and `verify` | Independently repeated installed-route, Docker, source, configuration, kernel, listener and enrollment observations for one supported manager/proxy pair. | It requires the current single-pair profile and an independently held exclusive original owner fence. Additional management-like objects are refused. |
| `fresh_begin.begin_new_instance` and `inspect_new_instance_origin` | A durable, source/root-bound NEW origin before first launch. | `NewInstanceRequest` has no original-history authority binding. NEW original start requires the separately held private-engine profile. |
| `fresh_host_observation.serve_one_fresh_grant` | An authenticated local peer joined to a selected container, original shared fence and supervisor lifetime. | It is the existing fresh protocol, not a deployment-wide recovery epoch issuer, and it has no complete controller/deployment fence-transfer receipt. |

The declarations above describe actual functions, not replacement interfaces.
The implementation of #2824 must invoke and reverify the applicable original
producers. Copying their serialized output into a new receipt does not enlarge
what that output proves.

## Demonstrated integration gaps

### A prospective frontier does not contain its baseline's origin

`original_attempt_evidence.derive` selects the first anchor's
`(old_instance, old_container_id)` as `baseline`. Later unanchored predecessors
must match that pair, but no corresponding attempt is created or derived for
that baseline. `original_attempts.enroll` does not inspect prior activity and
correctly describes itself as beginning a prospective scope.

The observable counterexample is an original manager A, an earlier unrecorded
failed successor X, later enrollment L, and a recorded A-to-B attempt. L can
correctly enumerate all attempts captured after its enrollment, but the
unchanged A-to-B frontier contains no evidence deciding whether X existed.
Backward traversal from B and present stopped-state inspection cannot supply
that absent reverse completeness.

A genuine NEW origin is useful evidence about A, but its current request does
not bind L before A's first-start authority. Joining A's NEW receipt to a later
L enrollment still leaves the uncovered interval. The join must be recorded by
an actual original admission producer, before the earliest relevant launch or
attempt, and enforced by every supported successor admission route. A new
recovery record cannot backfill that event.

There is a second, separate history distinction. With complete original store
receipts, `derive` still adds an unavailable reason when
`read_fresh_status(..., host=True)` is not `succeeded`, and `verify` refuses any
unavailable reason. A recovery composition cannot discard that refusal text.
If the supported recovery profile includes validly recorded unknown outcomes,
its authoritative evidence contract must distinguish complete original facts
with an unresolved outcome from missing or unverifiable original facts. Both
retain their original outcome; neither becomes a launch permission. Missing
expected stores continue to refuse unless the original producer's affirmative
terminal never-created boundary proves that exact exception.

### Current containment's owner lock conflicts with required shared custody

`containment_kernel_subobservation._external_owner_fence` requires an external
holder distinct from the observer and verifies a kernel `WRITE` flock on the
exact original owner file. REQ-104 requires the recovery issuer to hold a newly
acquired independent shared lock on every original owner file while it verifies
containment at the issuance boundary. Those two locks cannot coexist on the
same Linux file.

An isolated kernel probe using a real exclusive holder and a separate process
that independently opens the same file observed `LOCK_SH | LOCK_NB` refuse
while the exclusive lock was held, then acquire after its release. This is
positive evidence of the composition conflict, not Docker or recovery
acceptance. Duplicating or inheriting the exclusive holder's descriptor would
not establish the independent shared acquisition required by REQ-104.

A recovery observation profile therefore needs a defined, independently
verifiable shared-fence contract. It must recheck the exact file, kernel owner,
process/boot lifetime and current custody without weakening the existing
exclusive-fence profile or asserting coverage of the earlier gap.

### Single-pair, source and engine profiles do not currently join

`containment_observation.observe` refuses a management/proxy-like Docker object
outside its selected pair as `docker-alternative-management`. A recovery
frontier can contain multiple retained ancestor and failed-successor objects;
the selected consumer may also exist when the grant is issued. Filtering those
objects out before calling the verifier would hide exactly the scope that
REQ-101 through REQ-103 require it to establish.

`installed_bootstrap_source.inspect_installed_bootstrap_source` recognizes two
explicit original bootstrap digests:

- Ordinary original: `66bcfefa86aa2bd88dd4a253405fc6faac65675917e8d393e587d5a62fff83bb`.
- Quarantined original: `6a7da1f8f57aab76acb062a911598f75f4ea0b237308389f3807bc6368785e59`.

A matching image name, caller digest, current checked-out source or successful
original-history test is not another recognized source profile. Meanwhile,
`original_attempt_evidence.derive` requires the enrolled source identity to
match `original_attempts.source_identity()` of the verifying package. The
supported set of original source profiles must be explicit; it cannot be
inferred from unrelated component success.

Finally, `fresh_new_host_start.start_new_docker_once` requires an independently
verified private Docker Engine and its recorded `DOCKER_HOST` socket. Current
containment source and route discovery reject `DOCKER_HOST`/`DOCKER_CONTEXT`
overrides and inspect the local system engine. Running one verifier against the
outer engine and another against an unbound inner engine does not prove a
single deployment's ownership or controller scope.

## Minimal prerequisite capabilities proposed for review

These are proposed capability boundaries, not newly published Issues,
authorizations, changes to #2824's Objective, or waivers of its Requirements.
The existing recovery issuer remains responsible for REQ-101 through REQ-108.

**Prospective deployment genesis and complete lineage bridge.** Establish an
original, protected binding between a never-before-launched deployment origin
and its attempt history before any supported first-start or successor authority
can escape. The producer must carry that binding through ordinary and failed
successors and refuse a missing or advanced frontier. Its independent verifier
must prove the baseline and all recorded successor branches from original
facts, preserving unknown outcomes and distinguishing incomplete facts from
known unresolved outcomes. The output remains evidence-only. Historical
pre-enrollment deployments and unsupported source profiles remain unavailable;
there is no legacy conversion.

**Current containment for a complete recovery deployment.** Independently
observe the authorized set of original generations, their proxies and all
ordinary controller routes against the same engine/source profile as that
history. Define issuer-held independent SH fences and separately retained
controller/deployment custody, preserve exact current namespace/resource pins,
and repeat the observations in the actual issuer invocation. An additional
container is accepted only when its exact role and original or target binding
are verified; an omitted or unrecognized controller still refuses. This adds
a recovery-specific contract without relaxing ordinary fresh admission or the
quarantined-original distinction.

These are independent producers. The lineage bridge can produce correct
non-authorizing original evidence without a recovery grant. Current
containment can produce correct non-authorizing observations without issuing
or consuming an epoch. Their state/lifetime and runtime authorities differ.
The host issuer should compose their defined outputs at one durable issuance
boundary rather than silently invent both producers inside a nominal adapter.

**Unresolved supported-profile decision:** choose the production origin and
engine model for the first supported complete recovery deployment. Two concrete
paths are a prospective genesis path using the local system engine, with its
own explicit closed-controller admission, or extending the genesis/lineage and
containment observers to the existing private-engine NEW origin. The former
changes the supported genesis admission surface; the latter must bind both
outer private-engine custody and inner deployment facts. Existing component
code does not select between them. Neither option authorizes migrating,
re-enrolling or launching the current incident's deployment.

## Requirement-by-requirement implementation audit

| Requirement | Reusable implementation | Missing production fact or integration |
| --- | --- | --- |
| REQ-101 | Original history derivation/verification; quarantine journal reader. | Original baseline-to-history bridge, full ancestor scope, and explicit complete-but-unknown evidence semantics. No synthetic baseline or lost-journal conversion. |
| REQ-102 | Approved enrollment, installed source/mount verification, complete route observer. | One accepted source/engine profile covering all lineage generations and the selected consumer. Operator lists remain selections. |
| REQ-103 | Exact original process/cgroup observations, pinned namespace checks, listener and controller inspection. | Recovery-specific current SH/resource-custody observation repeated in the issuer immediately before grant. |
| REQ-104 | `acquire_prior_owner_fence`, protected file opens and `_assert_pid_flock`. | Issuer-owned independent SH acquisition for the exact complete original set, rechecked after acquisition and at issuance. |
| REQ-105 | Protected, exclusive, append-only persistence patterns; bounded recovery state prototype. | Deployment-fixed production inventory and complete evidence binding, then a single reservation/intent/consumption/receipt state machine reached by the real issuer. A prototype alone is not this gate. |
| REQ-106 | Existing local peer credentials, challenge and selected-container/owner checks; immutable READY validators. | Current whole-scope verification, complete consumer fence-custody receipt, durable one-use consumption before dispatch, and current host-lifetime/readiness handling. |
| REQ-107 | Existing old-root unknown-outcome and first-launch refusals. | Explicit new-epoch authorization and original-exclusion composition that cannot reuse an old root, plan, claim or operation key. |
| REQ-108 | Real original-host boundary fixtures, Docker fresh tests and disposable closed-host fixture components. | All integrated AS-B01 through AS-B05 scenarios through the recovery production issuer and actual prerequisite verifiers. Existing component receipts do not satisfy this row. |

The durable prototype may verify persistence and locking behavior independently.
It does not make any missing row ready, and the absence of an issuer entry point
must not be presented as successful fail-closed completion of the Issue.

## Issuer and consumer protocol boundary

A prospective issuer needs a fresh authenticated local challenge bound to the
fixed deployment, explicit authorization, exact target/preparation and exact
consumer process/boot lifetime. The existing `SO_PEERCRED` and container/ledger
joins are useful mechanisms, but the recovery protocol must additionally prove
custody of the complete required fence set.

Original shared locks require independent acquisition by each relevant holder.
A passed or inherited descriptor must not be reported as that independent
acquisition. Controller/deployment exclusive fences have a different custody
question: if they are transferred using file descriptors, the issuer must wait
for a challenge-bound receipt and independently inspect the exact receiving
process's descriptors and kernel locks before consuming the epoch. A queued
`SCM_RIGHTS` message or successful `sendmsg` is not receipt. A bare caller claim
that it holds a descriptor is insufficient as well.

The target source/configuration and installed READY must be revalidated at that
same boundary using their actual validators. `ManagementVersionStager.lookup`,
`self_update_handoff.verify_installed_runtime` and `fresh_runtime_binding` are
existing sources to evaluate for the selected runtime profile; their contracts
must be matched, not replaced by the text `READY` or by an image SHA alone.
All original lineage, current containment, target and fence observations must
remain exact across final durable consumption and dispatch.

Consumption precedes any escaping grant. Lost response, interrupted receipt,
partial publication or host exit leaves the original state preserved and
non-reusable, including for another target/preparation. Current readiness must
become unavailable after host loss without rewriting a historical observation.
An already emitted grant may race the first launch: the testable requirement
is retained custody and no second authorization, not an impossible promise
that no `Popen` can occur after host death.

## Integrated production-origin test plan

| Scenario | Production origin and material barrier | Required observed outcome |
| --- | --- | --- |
| AS-B01 / REQ-101, 107, 108 | Genuine prospective genesis followed by A-to-B-to-C and an original refused successor; actual original verifiers inside issuer. Remove/mutate each baseline, anchor, store, expected failed record and attempt binding in separate preserved cases. | Positive complete scope; exact refusal for missing, stale, forged or cross-deployment facts. Genuine original terminal never-created control succeeds independently. No new historical record, cleared unknown outcome or store repair. |
| AS-B02 / REQ-102, 103, 108 | Full selected closed profile in a disposable systemd VM with real Docker, ordinary restart/watch/service/timer/cron/remote routes and a preserved worker. Observe actual cgroup and namespace membership before each action. | Every omitted, enabled or unknown route, escaped/nonleader task and occupied current listener/namespace refuses. Positive exited-task control is independently measured. Worker/configuration and all original stores remain intact. |
| AS-B03 / REQ-104, 106, 108 | Independent real host and consumer processes open exact original files. Pause at an observed acquisition/receive boundary, then have another process attempt target EX while the intended holds overlap. | Independent SH ownership and exact peer lifetime pass. Busy/replaced/missing files, inherited-as-independent locks, omitted/duplicate descriptors, queued-only transfer and wrong receipt refuse before grant. |
| AS-B04 / REQ-105, 107, 108 | Two real production-factory commands use different preparation/target names but one fixed inventory. Pause the first at a recorded reservation boundary and observe the second actually contend before releasing the first. Kill around each record/create/fsync/head boundary. | At most one reservation/consumption path, no alternate-root bypass, no accepted environment/request override, no repair or reuse of a partial/uncertain state. Preserve bytes and identities for independent readback. |
| AS-B05 / REQ-103, 105, 106, 108 | Pause immediately before the actual final grant boundary, mutate source/READY/target/controller/process/fence evidence, then resume. Separately lose replies or kill the host around consumption and dispatch. | Changed/stale input refuses; a consumed epoch never grants again. Host loss makes current readiness unavailable. If a grant already escaped, observe retained consumer custody and no second grant rather than assuming launch cannot race. |

`tests/fixtures/original_host_boundaries.py` demonstrates suitable barriers at
`before-event`, `after-event`, `after-pending`, `after-replace` and `after-sync`
around actual producer publication. Its subprocesses execute the production
claim/grant path; instrumentation observes or interrupts real operations.
Merely starting two processes concurrently does not establish contention.

`tests/test_management_fresh_end_to_end.py` supplies genuine Docker histories
and authenticated-child flows. It does not independently establish the full
closed deployment profile. Conversely,
`scripts/test_management_containment_closed_host.py` exercises actual producer
and separate-consumer containment commands, real routes and current kernel
controls, but does not issue a recovery grant or establish complete lineage.
The integrated test must join these authorities through the new issuer.

The closed-host fixture requires its preexisting protected synthetic-host
authorization, an initially empty KVM/QEMU VM with PID 1 systemd and the initial
host namespaces. It deliberately inhibits fixture host routes. It is not a
command to run on a populated production host. The existing Supervisor
`fresh-host` CI profile runs privileged Docker tests, but does not itself run
this full closed-host recovery scenario. New integrated nodes and their native
receipts must be collected and required by an appropriate isolated profile;
no missing-node or skip allowance may silently convert unavailable authority
into acceptance.

## Durable state component in this draft

`recovery_epoch_state.hold_recovery_epoch()` has no arguments. Its production
root is fixed at `/var/lib/auto-coder/recovery-epoch`; environment variables,
request paths, target names and preparation names cannot select another root.
The factory requires the approved Unix owner and protected, canonical directory
ancestors. It does not provision or repair a missing directory, inventory or
lock. An operator would have to separately review and install the inventory;
this draft performs no such operation.

The finite `EpochInventory` binds the exact root and lock device/inode identities,
deployment, authorization, sorted unique enrollment set, request, preparation,
Docker target, source digest and configuration digest. An `EpochBinding` must
match those fields and additionally binds the verified-evidence digest and the
actual current issuer boot/PID/start lifetime. The component validates those
joins and their exact schema, but cannot establish that the supplied evidence
was independently produced. The missing issuer must establish that fact before
calling it; inventing a digest is not an authorization mechanism.

An independently opened exclusive flock serializes the whole state invocation.
The component rechecks the current process's actual kernel lock, protected
inventory identity and bytes, every retained record and the complete namespace
before each transition. A forked or released holder, replaced lock/root/record,
changed inventory, conflicting alias or existing phase refuses.

| Transition | Persisted fact | Reentry behavior |
| --- | --- | --- |
| `reserve(binding)` | `reserved.json` and immutable `reserved-head.json` | Any subsequent invocation refuses this deployment state, including a different target/preparation. |
| `record_intent(binding)` | `intent.json` and immutable `intent-head.json`, linked to the prior head | A partial or uncertain write is preserved and cannot be repaired or resumed. |
| `consume(binding)` | `consumed.json`/head followed by `receipt.json`/head | Returns only after both transitions are durable. A lost return, exception or second call cannot produce another receipt. |

Each new file uses exclusive creation and no-follow opens, file fsync and
directory fsync. The append heads and records retain the exact binding and
previous-head digest. The library never deletes a partial file, replaces a
committed head, resets a reservation or reconstructs state after restart.
`EpochReceipt.grant_delivered` is always `False`: this component neither has a
transport nor observes a grant. The host issuer must retain and independently
reverify consumer custody and current evidence at its eventual dispatch boundary.

`_hold_recovery_epoch(root, expected_uid=...)` is an internal component-test seam.
It must not become a production CLI/environment override. The maintained tests
use isolated user-owned files and real process/kernel flocks. They exercise
material contention after a post-reservation barrier, I/O failures around each
record/head fsync, SIGKILL at observable publication barriers, changed bindings,
path replacement and replay refusal. These are bounded persistence tests, not
production-host, Docker, complete-lineage or integrated grant acceptance.
Process interruption tests do not claim simulated storage-power-loss durability.

Run the maintained component selection with:

```sh
bash scripts/test.sh --ci-tests-only tests/test_management_recovery_epoch_state.py
```

The component uses the existing Loguru configuration with console and rotating
file sinks. Its phase records explicitly carry `grant_delivered=False`; they
never report authenticated readiness or recovery success. The eventual issuer's
production trace integration remains part of the unfinished contract below.

## Observability and completion boundary

The unwired persistence/design work adds no production processing origin,
admission grant, success event, worker action or dashboard-owned state.
Consequently it supplies no recovery-success trace and no dashboard claim of
readiness. Wiring the eventual issuer must include its real reservation,
refusal, consumption, dispatch and current-host-loss observations in the
production trace contract and `docs/dashboard-observability.md`, with runnable
tests for those boundaries.

Do not close #2824, merge an implementation as its completion, or deploy a
recovery issuer until the supported-profile decision and missing producers are
resolved, all eight requirements are actually connected, and the integrated
native evidence passes. Completion of a prototype or this audit is not that
result.

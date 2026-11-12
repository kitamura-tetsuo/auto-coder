# Management recovery ownership epoch

The host producer in `management/recovery_epoch.py` establishes one new,
explicitly authorized ownership epoch for the finite ordinary local-system-Docker
profile. It joins original provenance from #2839, current full-scope containment
from #2840, quarantined operation inspection from #2823, genuine installed READY,
and actual receiving-process fence custody. Each prerequisite remains evidence
only. None can issue a grant or repair a missing original record.

The separate #2825 consumer owns a durable child-launch claim, one child launch,
authenticated application readiness, and quarantined admission/dispatch. This
producer's `--recovery-epoch` bootstrap endpoint acquires and retains custody but
never starts a child, creates an operation store, or announces readiness. A
successful socket send is not proof of delivery or an application outcome.

## Supported original and current scope

A supported deployment begins with the affirmative original root-creation and
ordinary-bootstrap genesis producers described in
`management-original-genesis.md`. Later supported attempts remain in that same
protected history frontier, including failed branches. Complete original facts
can retain an unknown outcome. A missing expected journal, missing genesis,
unsettled original SQLite sidecar, changed file, or external legacy baseline
remains unavailable. Current enrollment cannot supply historical completeness.

The current scope is one approved local system Docker engine, exact ordinary
manager/proxy pairs, every actual watcher and normal restart route, and one
separate prelaunch target. Every existing selected original owner inode must be
independently held SH by the issuer and consumer. The issuer also acquires the
actual existing watcher locks EX and its deployment-fixed epoch lock EX. It
transfers those exclusive open file descriptions and the actual retained nsfs
objects, then independently observes their presence in the authenticated
consumer's `/proc/PID/fd` and kernel lock data. Queued SCM_RIGHTS descriptors or
`sendmsg` success do not establish receipt.

Original lifetime/cgroup/network evidence comes from actual capture while the
original processes were available. A current namespace pin holds the actual
kernel object. A saved historical inode or Docker's stopped status cannot replace
that object, all-task inspection, current namespace occupancy, or endpoint
listener absence. A positively recorded never-created attempt may have no current
Docker object; its exact absence must be present in full-engine observations and
its original failed branch remains retained. Existing objects cannot be omitted
from enrollment to obtain that result.

The target retains the original OAuth root and inode. Its state and journal roots,
Docker identity, management instance, and published host port are distinct from
every original. The issuer does not stop/start containers, inhibit controllers,
change OAuth, reset old roots, or touch workers. Those conditions must already
hold under separate reviewed operational authorization. Unknown controller routes
or unsupported profiles refuse. NEW/#2672 and the existing ordinary/quarantined
observation profiles retain their admission contracts.

## Deployment-fixed authority

The host CLI is `python -m auto_coder.management.recovery_epoch`. It takes no
request, inventory, authorization, or state-root argument. Production files are
fixed; test-only internal factories are not CLI alternatives.

| Location | Required original authority |
| --- | --- |
| `/var/lib/auto-coder/recovery-epoch/inventory.json` | Canonical version-two `EpochInventory`, including exact directory/epoch-lock identities, deployment, authorization, one enrollment, request, preparation, Docker target, bootstrap/source digests and the full authorization digest. |
| `/var/lib/auto-coder/recovery-epoch/epoch.lock` | Preexisting protected original file; independently acquired EX. The issuer never creates or repairs it. |
| `/etc/auto-coder/recovery-epoch/authorization.json` | Canonical `EpochAuthorization`: exact history binding, protected consumer and READY file identities, configured source repository/read credential/installer/interpreter, exact tested source and socket-directory identity. |
| `/var/lib/auto-coder/approved-recovery-containment-enrollments/<id>.json` and its approved routes file | The complete scope independently checked by the current-containment producer. An operator list is not a containment verdict. |
| `/var/lib/auto-coder/recovery-epoch-peer/issuer.sock` | One protected authenticated local endpoint, created only inside the separately provisioned fixed private directory. Existing endpoints refuse; no stale-socket repair is attempted. |

Authority directories are root-owned private canonical directories with trusted
ancestors. Files are private original single-link files. The inventory's
`authorization_sha256` binds the complete separately reviewed scope, and that
scope must still match the inventory and original inode/bytes at issuance.
`AUTO_CODER_RECOVERY_EPOCH_ROOT`, `AUTO_CODER_RECOVERY_EPOCH_INVENTORY`,
`AUTO_CODER_RECOVERY_AUTHORIZATION_ROOT`, and
`AUTO_CODER_RECOVERY_CONTAINMENT_ROOT` are refused rather than used as overrides.
No provisioning or alternate-root deduplication path exists in the issuer.

Version one remains a non-authorizing persistence component format; it cannot
enter the complete issuer. Version two requires canonical retained evidence and
its matching digest. An original request or genesis authorization cannot be
reused as the new recovery request/authorization.

## Exact installed target and receiving process

The target selects `BootstrapConfig.recovery_epoch_config` as
`/bootstrap/recovery-epoch.json` and explicitly invokes:

```sh
python -m auto_coder.management.bootstrap_supervisor \
  --config /bootstrap/config.json --recovery-epoch
```

The ordinary supervisor constructor refuses this selected profile, so omitting
the mode or entering the normal first-child path does not bypass recovery. The
endpoint opens the already provisioned target `.supervisor-owner.lock` without
creation, obtains its own independent EX, and requires no previous target launch
or operation state. It connects only to `/management-recovery/issuer.sock` and
holds its own independently opened original/history SH files.

The endpoint's private configuration binds request/authorization/target,
expected issuer UID, actual socket-directory device/inode, every original
resource name/path/device/inode, target-owner identity, and each transferred
controller/epoch/namespace identity. The issuer independently joins the complete
set to its actual held descriptors. The target mounts the fixed host peer
directory read-only at `/management-recovery`, the exact history lock read-only
at `/management-recovery-history/history.lock`, and selected original owner files
read-only at `/management-recovery-originals/<index>/owner.lock`.

The host authenticates the local peer with SO_PEERCRED, an independently opened
pidfd, the actual Docker leader PID, Linux start token and boot identity. The
consumer uses kernel SO_PEERPIDFD to pin the actual issuer across Docker PID
namespaces; a sender-supplied pidfd or claimed host PID is not authority. Kernels
without this capability refuse. Every resource check remains live after receipt.
The overall consumer verification wait is bounded by the configured bootstrap
verification timeout, up to one hour; individual exchange sends remain bounded.
SIGTERM/SIGINT immediately interrupt a pre-grant wait and close the non-launching
endpoint's resources. Host death does not silently release an already returned
consumer handle's descriptors: its owner must explicitly end custody when safe.

The selected initial runtime is `management-installed`. The issuer invokes the
actual `ManagementVersionStager.lookup` against original READY, current immutable
tested-source evidence and installed bytes. The management configuration and all
runtime arguments/environment/source identities must agree with
`runtime_for_receipt`. The READY preparation directory must equal the approved
preparation ID. Unsupported executable environment hooks refuse.

The installer pin retains the complete `uv --version` text after its `uv `
prefix, including any build descriptor. Its grammar is a numeric three-part
version with one optional parenthesized ASCII build descriptor, bounded to 128
characters in total. The stager still requires exact equality with the selected
installer's actual output; a matching numeric version alone cannot authorize a
different build. The native fixture verifies its separately selected numeric
version before passing that complete observed pin into authorization and READY.

Both the complete interpreter distribution and READY environment must be inside
the exact same-path read-only version-root mount. In particular, a host-only venv
interpreter symlink cannot satisfy target verification. The authorization's
configured interpreter and the runtime's resolved interpreter must be the same
file inside that closure. This permits an actual portable CPython installation
within the separately reserved version root; it does not rewrite a receipt's
original paths or accept an invented READY file.

## Single-use durability and grant ordering

The issuer first independently derives and verifies the full original lineage,
inspects all actual quarantined journals, obtains current containment, verifies
exact installed target READY, and observes the consumer's complete acknowledged
custody. It then records the fixed sequence:

| Phase | Durable consequence |
| --- | --- |
| `reserved` | The one deployment-wide invocation and complete original/current evidence binding are reserved. |
| `intent` | The exact target, authorization, source, full original fingerprints, controller/resource identities and receiving custody remain retained. |
| `consumed` | That epoch can never yield another grant, including after interrupted persistence or a lost reply. |
| `receipt` | A final durable record binds the consumed record and exact evidence; it is not itself delivery proof. |

Each phase uses O_EXCL publication, file and directory fsync, an immutable
hash-linked head, actual writer-inode readback and complete directory/inventory
revalidation. `evidence_json` retains the canonical verified original fingerprints
and current bindings, and must match `evidence_sha256`. The original outcome is
preserved in that evidence. A conflicting binding after reservation poisons the
current invocation, including when a caller catches the error. Cancellation or
uncertainty during publication also poisons it. Every later invocation rejects
existing or partial phase evidence; nothing resumes, resets or repairs it.

Original lineage, exact target READY, protected authorization/enrollment,
quarantined journals, actual current containment and receiving custody are all
rechecked before consumption and again after persistence, immediately before the
single send. The grant is bound to the fresh peer challenge and issuer receipt
nonce, exact request/authorization/target, four durable digests and actual issuer
lifetime. There is no retransmission path. A completed send followed by host
loss can race a future consumer's first launch; correctness requires retained
custody and no second grant, not an impossible promise that host death forbids an
already-authorized Popen.

After the one send, the host remains alive with its fences and session. Loss of
that host, socket or custody makes current consumer checks unavailable. The
historical durable receipt remains unchanged. This endpoint never marks an
application ready; #2825 must own that separate proof and protected operation
lifecycle.

## Observability and validation

The CLI and bootstrap endpoint use the existing Loguru stdout plus file sinks.
Durable phase events name `recovery_epoch_phase`, request and target. Explicit
`current_ready=False` accompanies grant-send and unavailable observations. No
GitHub issue/PR processing attempt, dashboard slot, management operation admission
or worker outcome is created by this host ownership protocol. See the matching
entry in `docs/dashboard-observability.md`.

Refusal diagnostics retain up to eight explicit exception causes with only their
type, integer error number, and code basename/function/line. They omit inner
exception messages, arguments, frame locals and full paths, and never change the
refusal decision or preserved state.

The maintained component selections cover real persistence failures/process
interruption, exact fixed-scope parsing, independent-process SH/EX/nsfs custody,
actual received descriptors, current peer lifetime and cancellation. Component
fixtures are not original historical authority or live Docker containment.

```sh
bash scripts/test.sh --ci-tests-only \
  tests/test_management_recovery_epoch_state.py \
  tests/test_management_recovery_epoch_authorization.py \
  tests/test_management_recovery_epoch_peer.py \
  tests/test_management_recovery_epoch_consumer.py
```

The separate isolated initial-namespace systemd/Docker VM acceptance must traverse
the actual genesis/attempt producers, complete current observer and exact host
issuer/consumer entrypoints. Its AS-B01 through AS-B05 obligations include
observable contention, missing/mutated/forged prerequisites, all-controller/task/
listener refusals and interruption/host-death boundaries. Passing the component
selection alone does not establish those native obligations or applicability to
a current incident with missing originals.

### Maintained native acceptance

`scripts/test_management_recovery_epoch_host.py` owns the actual host-entrypoint
driver and each scenario's required outcomes. The
`Management Recovery Epoch Acceptance` workflow derives its complete matrix from
that driver's `ACCEPTANCE_SCENARIOS`; its final job requires every scenario to
succeed. A missing case, failed VM, unavailable native kernel operation or failed
preservation check cannot become a passed result. The VM runner and reviewed
reproduction procedure are documented in `scripts/recovery_vm/README.md`.

Before recovery preparation, the fixture proves that all original A/B/C managers,
proxies and cutover readers have stopped. It may then close only the empty WAL
and derived SHM residue of their twelve known original stores, preserving every
database's bytes and identity. The separate A/B readiness observation stores are
required and preserved without checkpointing; unresolved C has none. Unknown
stores, nonempty WAL/rollback journals, or readiness sidecars refuse. This fixture
setup does not repair an operational recovery source or change the production
quarantine policy.

| Contract | Actual fixture action and independent outcome |
| --- | --- |
| AS-B01 | Run genuine genesis, successful successor, never-created failed branch and unresolved successor producers. Verify full original lineage, reject forged/cross-deployment/stale reports, remove an original journal, and mutate the retained failed branch's exact terminal evidence after intent. Refusal must preserve the original uncertainty and stores. |
| AS-B02 | Install the ordinary service/timer/watcher/alternate-root fixture routes, bind them in approved enrollment and verify their actual inhibition. Exercise unknown/omitted/startable controllers, retained namespace listeners, an escaped nonleader task and independently measured exit. Repeat the real containment verifier in a separate process and preserve unrelated worker claims and effect counters. |
| AS-B03 | Use the real host and Docker bootstrap processes, kernel peer credentials/pidfds and independent SH/target EX acquisition. Refuse busy/replaced/missing fences, a foreign acquiring process, queued rights without acknowledgment, received rights before acknowledgment, and a changed target lifetime. Later successful issuance must retain the complete observed custody. |
| AS-B04 | Reject actual CLI/environment root and inventory overrides. Stop the real issuer at its fsynced reservation head while a contender runs, then test conflicting target/preparation binding. In separate fresh VMs, SIGKILL the original process before and after every record/head publication and inspect the exact retained prefix and refused replay. |
| AS-B05 | After the original intent head, change exact configuration, READY, installed source, original evidence, watcher/controller state, fence acquisition or target lifetime. Require refusal before consumption. In a distinct fresh VM, change exact READY after the fsynced receipt head and resume the same issuer; require actual refusal and an empty authenticated receiver queue while the receiver is stopped. Preserve consumed records, the injected mutation and workers, and refuse replay. Separately require a real queued grant before issuer death, then resume the receiver and require unavailable current custody with no replay. |

Publication and protocol fault helpers only observe real syscall entry/exit and
kernel descriptors. They do not replace system-call results, inject verifier
verdicts or alter production code. Deliberate late fixture mutations remain
present through refusal and replay; their exported preservation baseline is
explicitly the state after that injection. The successful path measures original
OAuth/history/journal bytes and identities, unrelated beta/release/agent-table
worker ownership and effect requests, and the absence of any replacement child
or application admission. The current PR's custody endpoint and a separately
qualified installed READY are exercised independently; these checks do not claim
that the separate #2825 management-child launch/readiness contract is complete.

The separate `omit-post-consumption-verifier` mutation-control job proves that
the post-receipt scenario detects omission of the second final verification.
Before creating any original fixture, it copies the project into a fresh private
directory and removes exactly that one standalone call. An AST comparison
retains the first verifier and every other source expression; the original and
copied file digests are retained. The copied project is installed and supplies
every actual host process and fixture image, so source, history and enrollment
identities continue to agree without replacing an authority provider.

The control runs the same post-receipt READY mutation and the same no-grant
assertion. Its distinct `mutation-detected` result requires the named
`post-receipt-grant-forbidden` failure and a complete canonical grant observed
with `pidfd_getfd` and nonconsuming `MSG_PEEK` on the stopped receiver's exact
authenticated socket. A timeout, process kill, unrelated exception, unavailable
kernel operation or log message cannot satisfy that result. Only after recording
that incorrect dispatch does cleanup end the mutant issuer; the receiver must
still reject current custody with no child/admission, and the consumed epoch,
deliberate READY mutation and unrelated workers must survive refused replay.
This control does not count as production acceptance, and it never changes the
original source checkout or resets an epoch for another attempt.

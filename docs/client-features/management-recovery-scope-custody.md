# Current recovery scope and flock custody component

`auto_coder.management.recovery_scope_observation` implements a bounded,
non-authorizing slice of Issue #2840. Its frozen `RecoveryCustodyObservation`
has schema `recovery-scope-custody-observation-v2`, status
`observed-component-only`, and an always nonempty `incomplete` tuple. It is
deliberately a different type and schema from the existing complete
[single-pair observer](management-current-containment-observation.md).
The v2 schema adds stable unrelated-object inspection digests; reports lacking
that evidence refuse verification. It cannot establish recovery containment,
readiness, historical exclusion, original genesis, or permission to launch. No admission path consumes it.
REQ-601 through REQ-607 and AS-T01 through AS-T05 remain unchanged and are
not fully satisfied by this slice.

## Read-only interface and supported component boundary

The production Python interfaces are:

```python
from dataclasses import asdict
from auto_coder.management.recovery_scope_observation import (
    observe_recovery_custody,
    verify_recovery_custody,
)

report = observe_recovery_custody("deployment-name")
saved = asdict(report)  # Serialize to JSON through the caller's protected channel.
# A different process calls verify_recovery_custody("deployment-name", saved_json).
```

The verifier takes the JSON-decoded report, repeats all component observations,
and compares every stable field, including the missing predicates. The producer
must still have its original live PID/start token. The consumer must have a
different PID. The verifier retains the producer pidfd through evidence comparison
and checks its original start token and exit state again immediately before
returning. Producer exit, holder exit, a different boot, future or malformed
interval, and a report older than 30 seconds from sampling start refuse.
All intervals use the same boot's monotonic clock. An observation UUID is a
correlation identity, not a signature or an authorization credential; independent
current reads supply the facts. Neither a saved claim nor matching enrollment
can replace those reads. Rejected observations raise `RecoveryScopeUnavailable`.

This component requires initial-namespace Linux host root, unrestricted host
procfs, pidfds, and the local system Docker engine at
`unix:///var/run/docker.sock`, using the existing host-profile prerequisite.
Docker endpoint/configuration/TLS environment overrides refuse. Public callers
select only a deployment ID, never a filesystem root, profile, controller list,
or Docker endpoint. There is no installer, CLI, lock acquisition, controller
inhibition, Docker mutation, management-service operation, worker or OAuth action.
The module uses the host application's existing Loguru sinks, preserving its
colored filename/function/line console format and rotating file configuration;
it installs no sink or file during a read.

## Protected finite selection

A separate trusted operator supplies an existing deployment-fixed
`/var/lib/auto-coder/approved-recovery-scopes/<deployment-id>.json`. No such
enrollment is installed by this change. The canonical root is root-owned mode
0700, each canonical regular enrollment file is root-owned mode 0600 and at
most 128 KiB, and ancestors cannot be writable by group or others. Duplicate
JSON keys, unknown fields, aliases, path traversal, and unsupported profiles
refuse. The exact schema fields are:

| Record | Fields |
| --- | --- |
| Enrollment | `schema`, `deployment_id`, `profile`, `engine_id`, `originals`, `target`, `watchers` |
| Each original | `pair`, `owner` |
| Pair (including target) | `manager_id`, `proxy_id`, `manager_inspection_sha256`, `proxy_inspection_sha256` |
| Each watcher | `manager_id`, `config_path`, `config_sha256`, `lock` |
| Custody selection (`owner` or `lock`) | `path`, `device`, `inode`, `holder_pid`, `holder_start_token`, `holder_fd`, `holder_boot_id` |

The enrollment schema is `approved-recovery-custody-scope-v1`; its sole profile
is `ordinary-recovery-custody-component-v1`. This name does not declare an
accepted ordinary bootstrap source profile. There must be 1–16 original pairs,
one separate exact target pair, and exactly one selected watcher configuration
and existing watcher lock for every original pair and target pair. Additional,
missing, or duplicate watcher members refuse. All container IDs are distinct
full 64-character hexadecimal identities; inspection and configuration digests
are SHA-256. Existing owner/watcher paths and device/inode pairs cannot alias.
The target is never classified as an original or used to hide an original.

## Actual evidence origins and refusal cases

The reader enumerates **all** Docker objects, including stopped objects, without
label, image, or request filters. Duplicate identities in an inventory response
also refuse rather than silently collapsing repeated objects into a set.
The complete inventory is bounded to 512 distinct full container IDs and must
include every enrolled original and target. An unselected worker is admitted
only after independent inspection finds no selected image, management module or
management-serve command, selected manager/proxy network namespace, or bind-source
overlap with **any** original or target. Candidate discovery is shared with the
existing single-pair observer; its existing refusal behavior is preserved.
Unselected inspections require unambiguous image, effective command, network,
lifetime and restart facts. Docker `State.Status`, `State.Running`, and `State.Pid`
must agree: an exited/created/dead object cannot also claim to be running, and
a running/paused object cannot simultaneously claim to be stopped. When Docker
also supplies the independent `State.Paused`, `State.Restarting` and `State.Dead`
flags, each must be a boolean and agree with its corresponding status; contradictory
optional flags refuse rather than silently claiming an unrelated worker. Consistent
running, paused, stopped and dead workers remain permitted when otherwise unrelated.
This check does not establish continuous process lifetime or authorize recovery.
Candidate detection inspects module/management-serve
tokens even when embedded in a shell `-c` or Python `-c` argument; splitting
only on Docker's argv array would otherwise classify an explicit management
invocation as an unrelated worker. In this recovery-only component every
unselected Docker object must additionally expose complete actual `Path` and
`Args` launch facts. Both the declared `Config.Cmd`/`Entrypoint` and the
independent actual launch are classified: benign declared configuration cannot
conceal a management invocation in the actual launch. Missing, malformed or
ambiguous actual launch facts refuse. The existing single-pair observer's
readback requirement is not strengthened when both actual fields are absent.
This conservative command-text check does
not prove the absence of indirect wrapper execution, source aliases or hidden
controllers; full source and process closure remains an unmet requirement.
Bind roots must resolve canonically and must not lexically overlap or alias a
selected root's device/inode. Unsupported mount sources and ambiguous namespace
aliases refuse rather than being classified as unrelated. An operator's label or classification supplies no proof.

Engine identity and the complete inventory are reread, and the entire inspection
and classification pass is repeated while custody descriptors remain open. All
container IDs, including admitted workers, remain in `engine_inventory`.
`unrelated_inspections` stores each worker's stable full-inspection SHA-256; every
separate consumer repeats and compares it. A disappearing or swapped worker, or
a changed stable inspection, refuses. Only validated volatile `ExecIDs` on
independently classified unrelated objects are normalized, as in the existing
consumer. Selected full SHA-256 enrollment checks preserve that field. Workers
are never treated as management closure or enrolled controller members.
Digests otherwise preserve all fields and normalize only effective mount ordering
through the existing Docker canonicalizer. Enrollment snapshots select expected
current objects; they do not prove package source or a supported execution profile.

These two current samples cannot prove uninterrupted worker state between reads,
full REQ-601 through REQ-607 closure, original genesis, or authorization. This is a
bounded improvement toward REQ-601 and AS-T04 with deterministic component fixtures;
it is not native acceptance and does not complete either contract.

Bind sources must currently resolve canonically, and bind roots across original
generations and the target must not overlap or alias the same device/inode under
different paths. Root device/inode, size and timestamps are recorded and repeated;
a target root replaced between producer and verifier refuses. This does not
establish mount-namespace disjointness or exclude every nested filesystem alias.
Other mount types refuse. Each
original owner path must be exactly `.supervisor-owner.lock` in its actual
writable `/management-child-state` bind mount; nested state mounts refuse.
Each selected watcher configuration is independently read as protected mode-0600
bytes and matched to its enrolled digest, exact manager/proxy IDs and image IDs.
Its actual `state_root` must select precisely the enrolled `proxy-watch.lock`.
This uses the configuration fields consumed by production `proxy_watch_cli.py`,
whose reconcile and tick paths acquire that lock exclusively first. It does not
claim that every installed watcher origin or loaded watcher source was found.
That complete installed-controller/source observation remains unavailable.

All owner and watcher resources must already exist as canonical root-owned
mode-0600 regular files beneath protected ancestors. The observer opens them
read-only without `O_CREAT`, records inode/timestamp identity, and checks the
external holder's boot, PID start token, pidfd, exact FD identity, `/proc/locks`
and `/proc/<pid>/fdinfo/<fd>`. The original owner requires a whole-file **READ**
flock; every watcher requires a whole-file **WRITE** flock. Both the kernel lock
and the enrolled FD must name the holder's own acquisition. A forked child
retaining its parent's shared description is refused as an independent acquirer.
Reopening a different descriptor on the same inode is also insufficient.

All resource checks are repeated after the second Docker/configuration/enrollment
read while the observer's read descriptors remain open. Missing, unlocked,
closed, reopened, replaced, wrong-mode, foreign-boot, stale-lifetime and dead-holder
resources refuse. Opening a resource here does not acquire or preserve its flock.
No descriptor is retained by the observer after return, no exclusion is asserted
between samples, and release/reacquisition between samples cannot be ruled out.
Current observations supply no exclusion during any historical gap.

## Regression evidence and remaining acceptance

Run the bounded component and directly affected legacy boundaries through the
repository test runner (with normal project dependencies installed):

```sh
bash scripts/test.sh --ci-tests-only \
  tests/test_management_recovery_scope_observation.py \
  tests/test_management_containment_observation.py \
  tests/test_management_containment_composition.py \
  tests/test_management_containment_kernel_subobservation.py \
  tests/test_management_containment_enrollment.py \
  tests/test_management_containment_route_manifest.py \
  tests/test_management_installed_bootstrap_source.py \
  -m "not supervisor_integration"
```

The new tests call the production Python entrypoints, use **real local Linux**
flocks, independent holder processes, pidfds, fdinfo and separate producer and
consumer PIDs, and prove material contention: shared acquisition overlaps an
existing shared holder while an exclusive contender fails, and both contenders
fail at an existing exclusive watcher lock. They reproduce malformed unknown stopped
members, omitted target/controllers, resource aliases, overlapping target roots,
shadowed state, environment redirects, changed configuration/enrollment/inodes,
unlock during sampling, inherited descriptors, stale/foreign reports and
producer/holder death. Positive reads preserve fixture file bytes and locks;
a missing lock remains absent after refusal. Host-root protection and Docker
inspection inputs are explicitly substituted in these component fixtures.
Additional deterministic Docker fixtures admit an actual unrelated worker object,
refuse shadow images, management module commands, selected namespaces and overlapping
original/target bind roots, and detect disappearing/swapped workers and malformed
unselected facts. These fixtures never contact a Docker daemon.
Bounded protected enrollment JSON and serialized previous observations with
excessive nesting now produce typed non-authorizing refusal, rather than an
uncaught recursive decoder/encoder exception. These are component-boundary
regressions, not evidence of native recovery containment.
They are not end-to-end or native OS/Docker acceptance results.

Validation on this isolated worktree used Python 3.12.16 and locked dependencies.
The bounded local selection (new component, legacy observer excluding its
`supervisor_integration` Docker-mutating case, composition, kernel component and
installed source) passed 169 tests before the final additional component cases.
The final component-only run passed all 73 cases, including kernel-file root
aliasing and target-root inode replacement. Black, isort, flake8, the canonical
package-wide mypy invocation (359 source files), and repository hygiene passed.
The broader seven-file attempt returned 219 passes and 59 failures: 48 fixtures
attempted prohibited writes directly under the account's `/home/ubuntu`, ten
route/source cases refused this workspace's group-writable ancestors (including
three expected-reason mismatches), and the real Docker case failed its initial
`docker info` read before reaching any mutation. No fixture was weakened or
relocated to bypass protected-path checks. Those failures are retained as
unmet validation, rather than reported as passes. Subsequent bounded runs omit
the unsupported native case and do not retry outside-worktree fixtures. The test
runner, UV cache, dependency environment, logs and temporary files for the local
runs are confined to this worktree. Static checks are run separately when using
the explicit `--ci-tests-only` runner mode.

No supported real initial-namespace systemd/Docker VM acceptance was executed
for this slice. Completing #2840 still requires a separately reviewed integration
that supplies and repeats these missing authorities, without promoting this
component's status:

| Contract | Still missing |
| --- | --- |
| REQ-601 | Complete source/configuration/controller identities and native validation of candidate/source classification with unrelated objects. |
| REQ-602 | Actual image/package source closure, ordinary history-capable bootstrap recognition, effective configuration and command/mount profile validation. |
| REQ-603 | Union-wide system/user systemd, timer/socket/path, cron/at, local/remote-script and Docker restart discovery and actual inhibition; all installed watcher sources/configurations. |
| REQ-604 | Exact original boot/process/cgroup and remaining-task evidence, retained actual namespace descriptors, endpoint absence, target namespace disjointness, distinct never-started handling. Missing owner files currently refuse; no historical lifetime or owner file is invented. |
| REQ-605 | Joining these actual flock reads to complete installed-controller and retained namespace/resource custody. They do not establish historical exclusion or acquire consumer fences. |
| REQ-606 | Complete typed containment composition plus native production-entrypoint multi-generation acceptance, failed branches, escaped/nonleader tasks, occupied namespaces/listeners and preserved unrelated workers. |
| REQ-607 | Full supported recovery-profile reproduction and native evidence. The component interface, origins, refusal reasons and trace neutrality are documented here. |

AS-T01 through AS-T05 therefore remain native acceptance obligations. Their
future reproduction must use genuine original generations (including failed
branches), the exact target, actual installed routes and controller locks,
retained namespace FDs and overlapping custody in an isolated initial-namespace
systemd/Docker VM. It must test source/configuration and engine substitutions,
omitted routes/objects, old escaped/nonleader tasks, occupied listeners/namespaces,
producer/holder death and mutation between separate observations. The older
#2827 host harness is not evidence of recovery-scope acceptance. Original genesis
and historical coverage belong to #2839; the provenance join and grant consumer
remain separate work. No original record or genesis source is changed here.

## Observability assessment

This slice is observability-neutral: it is an independent read-only Python
component with no caller in operation admission, dispatch, durable recovery,
readiness, or dashboard event production. The candidate/source classification and v2 diagnostic report remain inside this
uncalled component; they introduce no production trace schema or dashboard
projection changes. `test_component_has_no_execution_or_recovery_events` compares
the real collector snapshot under an active execution scope before and after
successful independent verification and a real unlocked-controller refusal;
the snapshots remain identical. Only ordinary Loguru diagnostic messages are
emitted. No readiness, operation admission, dispatch or recovery-success event
may be inferred from `observed-component-only`.

Legacy ordinary/quarantined-original source allowlists, their exclusive owner
fence acceptance, and NEW/private-engine admission remain unchanged. In
particular, accepting a shared owner flock in this separate component does not
allow it to satisfy the legacy exclusive-fence profile.

The bounded unrelated-container classifier treats both `python -m auto_coder.cli management serve` and the installed `auto-coder management serve` console script (including shell-wrapped invocations) as management candidates. A different helper executable containing `auto-coder` as a name prefix does not by itself establish a management server. For an unrelated worker with nonempty `ExecIDs`, the observer now makes read-only `GET /v1.41/exec/{id}/json` calls over the locally bound Docker Engine Unix socket (after validating `GET /v1.41/info` against the enrolled Engine ID), binds the reported exec ID and parent container ID, validates its command and refuses management entrypoints. Duplicate or ambiguous exec records refuse; only independently classified unrelated exec identifiers may be normalized as volatile. The separate producer/consumer observations repeat these reads. Component regression fixtures explicitly substitute exec inspect, so they are not native exec-route acceptance. An exec that starts and exits entirely between sampling points is not ruled out. This is only candidate detection; it does not establish current source, controller, or namespace closure.

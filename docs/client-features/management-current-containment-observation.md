# Read-only current management containment observation

`python -m auto_coder.management.containment_observation` independently observes a
separately approved current management deployment. A complete observation has
status `observed-current-containment` and exit code **0**. Any missing, changed,
uninspectable or unsupported authority produces `unavailable` and exit code
**2**. Partial components never satisfy the complete result.

The result is current evidence, not historical continuity, an admission decision,
a recovery action or permission to launch. The observer creates no enrollment,
inhibits no controller, acquires no owner lock and retains no namespace resource
after return. The existing fresh-host replacement, admission and journal rules
remain separate. Successful component tests do not constitute real-host acceptance
of Issue #2827; the full ordinary and quarantined host tests remain a merge gate.

## Scope and CLI

A scope file selects exactly one proposed deployment and original container pair:

```json
{
  "deployment_id": "selected-deployment",
  "enrollment_id": "approved-enrollment",
  "authorization_id": "separate-operator-approval",
  "profile": "ordinary-v1",
  "management_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "proxy_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "files": []
}
```

The supported selectors are `ordinary-v1` and `quarantined-original-v1`. Selectors,
operator file lists and names do not establish approval. Duplicate keys, unknown
fields, unsupported profiles, abbreviated container IDs and relative paths refuse.
The optional sorted unique `files` list can select up to 32 canonical regular files,
each at most 1 MiB. Reports expose identities and SHA-256 digests, not file contents.

```sh
python -m auto_coder.management.containment_observation observe \
  --scope /private/scope.json > /private/current-observation.json
python -m auto_coder.management.containment_observation verify \
  --scope /private/scope.json --previous /private/current-observation.json
```

The `current-containment-observation-v11` report contains the exact scope and hash,
a fresh observation ID, boot/process lifetime and monotonic sampling interval,
file identities, whole-engine Docker facts, protected enrollment, original source
and mount profile, watcher, approved route inventory and original kernel checks.
All missing predicates have explicit unavailable reasons.

`verify` runs the same independent observation path again. It compares the complete
stable scope, authority and kernel identities; it validates sampling intervals
without requiring two fresh samples to have identical timestamps. The original
producer may have exited. Its saved assertion cannot replace any current read.
Cross-deployment, enrollment, authorization, profile, boot, file, container,
controller or holder substitutions refuse. A saved launch-grant status is invalid.
A new consumer observation is successful only while every required external
namespace pin and exclusive owner-lock holder remains independently inspectable.

## Authority and supported host

Observation requires initial-namespace Linux host root, host PID/user/mount/network
visibility, unrestricted procfs and cgroups, and the local `/var/run/docker.sock`
engine. Docker endpoint overrides and remote contexts refuse. A separate VM has
its own initial host namespaces; ordinary nested Docker does not satisfy this
host contract.

Docker reads use context inspection, engine identity, unfiltered container lists,
container inspection and bounded copies of the exact reviewed bootstrap file.
The complete engine inventory is read twice. Unselected containers using selected
images, management commands, a shared management network namespace or overlapping
original bind roots remain potential ordinary routes and cause refusal. An
operator cannot hide an alternative by omitting it from the scope file. Busy or
otherwise unsupported hosts can conservatively refuse; no global host filter is
inferred from a selected pair.

Approval is a separate trusted-host-operator action. The observer reads
`/var/lib/auto-coder/approved-containment-enrollments/<enrollment-id>.json` from a
canonical root-owned mode-0700 directory. The file is a root-owned mode-0600 regular
file. The exact enrollment, protected original files and prior lifetime evidence
are independently reopened with device/inode, timestamps, size and hash checks.
An inode replacement containing identical bytes is still a change.

A separately approved **present-time enrollment** is valid authority; it need not
prove past execution continuity. Root ownership alone is not a substitute for the
operator's separate approval. The record selects the exact deployment, image,
container, configuration and original lifetime scope, and carries
`approved_observation_only: true`.

The enrollment versions have distinct technical capabilities:

| Schema suffix | Independently observable resource | Complete result |
| --- | --- | --- |
| v1 | Protected selection and original files/lifetimes | Insufficient: no retained namespace or owner fence |
| v2 | v1 plus a live external original network namespace FD | Insufficient: no external exclusive owner fence |
| v3 | v2 plus a live external exclusive flock on the exact original owner inode | Eligible only when all source, route and kernel checks also pass |

The namespace holder must open the real original nsfs object while the manager is
alive and retain that descriptor through every observation. The owner-fence holder
acquires its exclusive lock after the original manager stops. Both are separately
authorized operator activities. Their boot, PID/start token, descriptor and object
identities are bound in the protected enrollment and independently rechecked with
PIDFD/kernel readback. The observer never creates those resources on demand.

## Exact original source and effective execution

Only the following reviewed bootstrap bytes are recognized:

| Profile | Bootstrap SHA-256 |
| --- | --- |
| ordinary-v1 | `66bcfefa86aa2bd88dd4a253405fc6faac65675917e8d393e587d5a62fff83bb` |
| quarantined-original-v1 | `6a7da1f8f57aab76acb062a911598f75f4ea0b237308389f3807bc6368785e59` |

The quarantined bytes match Git revision
`1b23f3be30ca47926462e7d261ef98e79bb82728`, blob
`594bec30aafa56852b9d9db3035718c7a1f9e887`. Both reviewed implementations acquire
`.supervisor-owner.lock` exclusively before entering their launch path. These are
exact byte profiles, not an allowlist of arbitrary images with similar commands.
The installed bootstrap is copied twice from the selected container's
`/opt/auto-coder/.venv/lib/python3.12/site-packages/auto_coder/management/bootstrap_supervisor.py`.
The bounded single regular tar member must match the protected enrolled source.

The mount reader binds the full current Docker inspection to the original captured
mount digest, exact command, configuration, security options, state/OAuth paths and
owner inode. Both profiles reject ambiguous Python environment resolution and
mounts shadowing the interpreter, package, `/workspace` working directory or user
package paths. Examining one file is insufficient if the command could resolve to
a different mounted module.

The ordinary profile supports the original directory mount at `/bootstrap` and
its reviewed supervisor command. A command retaining `--fresh-initialization`
after successful initialization is supported only with independently read,
protected intent, launch claim, launch and successful outcome records, matching
configuration hashes and original state/journal/OAuth/guard/owner identities.
The initialized container identity is recorded separately from the current
supervisor identity: the reviewed source supports reconstruction after successful
initialization. Current lifetime evidence must still identify the actual current
container and process.

The quarantined-original profile separately checks the exact fresh-initialization
command, `no-new-privileges`, read-only root filesystem, individual read-only
`/bootstrap/config.json` and `/bootstrap/management.yml` mounts, writable original
state/OAuth mounts and read-only `/management-versions` and `/management-source`.
Its configured installed runtime must resolve under `/management-versions` and
reference the protected management configuration. Healthchecks, unsupported
security/mount effects and bootstrap executable/environment references to the support
mount refuse. Observation-specific capture and stop checks cover this original
profile without relaxing fresh replacement admission.

A support-source original-file entry refers to a protected regular JSON descriptor
of the exact read-only support directory, rather than treating a directory as a
regular file. Its schema is `approved-quarantine-support-directory-v1`, purpose is
`non-executed-bootstrap-reference`, destination is `/management-source`,
`read_only` is true, and source/device/inode bind the actual canonical directory.
The effective runtime and bootstrap resolution checks must agree with that role.
This establishes the supported current mapping, without claiming no historical
process ever executed from the directory.

Both original profiles require a real Caddy proxy with its reviewed
`caddy run --config /etc/caddy/Caddyfile --adapter caddyfile` command and a protected,
read-only, unshadowed Caddyfile bind. An unrelated `sleep` process or an unattached
file labelled proxy configuration cannot satisfy the original proxy profile.

## Closed ordinary-route inventory

The separately approved route file is
`/var/lib/auto-coder/approved-containment-enrollments/<enrollment-id>.routes.json`.
It has the same protection rules and binds the exact enrollment hash and scope.
The v2 schema `approved-management-ordinary-routes-v2` is required for complete
closure. The older v1 component remains readable but cannot close user-controller
or deployment-root scope.

The reader independently enumerates and compares:

- The entire installed and loaded host systemd service, timer, socket, path,
  target, mount, automount and swap catalog, effective properties, unit text,
  drop-ins, dependencies and mask identities.
- Local accounts, user-manager lifetimes, linger/runtime directories, installed
  user unit trees and the actual live user managers' effective units and UnitPath.
  An uninspectable bus, manager, source tree or unknown/missing route refuses.
- Every Docker container ID and every actual selected manager/proxy bind source,
  including canonical path, device/inode, mode, destination and writable effect.
- Fixed `/etc/cron*`, `/etc/anacrontab` and `/var/spool/{cron,at,atjobs}` roots,
  protected local repair/recreate scripts and their independently observed
  inhibiting controllers.
- Supported local executable, script and configuration inputs referenced by
  controller commands. Their current bytes/identities are bound rather than
  assuming an unchanged unit file implies an unchanged wrapper.

The trusted operator reviews the entire catalog and every `restarter`/`unrelated`
classification. Known management sources, names and overlapping deployment roots
cannot be classified as unrelated. Each restarter must be masked, inactive, PID 0
and free of pending daemon reload, with the expected protected `/dev/null` mask.
Cron restarters need inhibited installed schedulers. Local script routes need an
observed inhibited controller. Nonempty remote script origins refuse until an
independent authenticated remote-controller reader exists. Root review of absent
origins and unrelated packaged services is an explicit trusted boundary; this is
not arbitrary-program analysis or protection against future privileged changes.

The fixed `auto-coder-management-proxy-watch.service` is checked independently of
the catalog. Its protected mask and current systemd state must agree with the
approved route inventory. Known local watcher source/configuration inputs are
independently bound when present; a mask can hide the older ExecStart text.
A stopped Docker pair alone cannot
satisfy a still-enabled watcher, timer, socket or repair route.

A read-only proposal is available:

```sh
python -m auto_coder.management.containment_route_manifest propose \
  --scope /private/scope.json --closed-profile
```

The draft has `status: unapproved-root-review-required` and
`complete_ordinary_routes_attested: false`. It is not accepted authority. An
operator must audit classifications, supported local inputs, omitted/remote
origins and inhibition before separately installing an approved record. Neither
proposal nor observation modifies routes, user sessions or approval records.

## Original lifetime and kernel checks

The kernel component reopens protected original manager/proxy lifetime evidence
and checks exact Docker start/finish/restart identity, current stopped state and
all original cgroup tasks. It scans every host process and nonleader thread for
the original pinned network namespace and checks the original host listener.
Escaped tasks, inaccessible observations, uncertain zombie lifetimes, occupied
namespaces/listeners and interleaving identity changes refuse.

During the scan the observer temporarily holds a reopened descriptor to the real
externally retained nsfs object. It independently checks the exact holder's live
exclusive `FLOCK ADVISORY WRITE` on the original owner inode, without taking a
lock itself. Docker, original evidence, pin and owner holder are read again after
the scan. A final complete observation pass also rechecks enrollment, selected
files, Docker, source/mount mappings, watcher, routes and stable kernel identities.
The observer closes its own descriptors on success and refusal.

A saved result retains neither resource. Releasing a pin, owner flock or holder,
replacing a descriptor/file, changing a controller or altering a scoped config
causes a later independent verifier to refuse. The report cannot reserve future
absence or authorize a replacement process.

## Validation and disposable real-host acceptance

Bounded automated regressions are run through the project runner:

```sh
CI=true bash scripts/test.sh --ci-tests-only \
  tests/test_management_containment_observation.py \
  tests/test_management_containment_composition.py \
  tests/test_management_containment_enrollment.py \
  tests/test_management_containment_route_manifest.py \
  tests/test_management_containment_kernel_subobservation.py \
  tests/test_management_installed_bootstrap_mount_profile.py \
  -m 'not supervisor_integration'
```

Composition tests inject component contracts to exercise status, replay, time and
failure propagation. They are protocol tests, not full-host success evidence.
The older `scripts/test_management_containment_enrollment_host.py` remains an
explicitly authorized partial-component probe with a synthetic sleeping proxy;
its image-source option checks source bytes only. Full mount/proxy acceptance is
owned by the separate closed-host driver.

`scripts/test_management_containment_closed_host.py` uses real Docker managers,
a real Caddy proxy, actual systemd routes, real namespace descriptors/owner locks,
and separate production `observe`/`verify` CLI processes. Successful scenarios
must return 0 with every required component; deliberate route, source, lifetime,
listener, pin and replay changes must return 2. Initialization/ledger facts come
from the exact original supervisor. The quarantine fixture prepares a genuine
installed runtime using the published tested-source record, public GitHub
readback and synthetic OAuth; no fake READY receipt or bootstrap substitution is
accepted as original-source evidence.

This driver requires a separately provisioned root-private
`/etc/auto-coder-disposable-containment-host.json` matching its authorization ID,
current machine/boot/Docker identities and `synthetic_only: true`. It also requires
an otherwise empty Docker inventory, an absent fixed watcher and an actual
QEMU/KVM VM. The driver does not install its own authorization marker or turn a
production host into a disposable test environment.

The concrete VM package is under `scripts/containment_vm/`. Preparation exports
only immutable preexisting images and the committed source; it does not launch
a VM. Its Ubuntu cloud disk is SHA-256 pinned. The runner uses Docker Compose,
2 vCPUs, 3 GiB guest RAM (4 GiB container limit), a 24 GiB sparse guest disk,
read-only input and a dedicated writable evidence directory. It exposes
`/dev/kvm` only to the temporary VM runner, with no host PID/network namespace,
Docker socket, production volume, account token or SSH port forwarding. The
guest uses public Ubuntu/PyPI/GitHub bootstrap access and its own Docker engine.

On the separately authorized host, prepare and build without launching:

```sh
bash scripts/containment_vm/prepare.sh \
  /var/tmp/ac-pr2829-vm-UNIQUE APPROVAL_ID
docker compose --env-file /var/tmp/ac-pr2829-vm-UNIQUE/compose.env \
  --project-name ac-pr2829-vm-UNIQUE \
  --file scripts/containment_vm/compose.yaml build
```

Under an explicit authorization covering this disposable fixture, run:

```sh
APPROVED_AUTHORIZATION_ID=APPROVAL_ID docker compose \
  --env-file /var/tmp/ac-pr2829-vm-UNIQUE/compose.env \
  --project-name ac-pr2829-vm-UNIQUE \
  --file scripts/containment_vm/compose.yaml up \
  --abort-on-container-exit --exit-code-from fixture
```

The guest writes separate acceptance results and diagnostic artifacts before
poweroff. A VM process exiting normally without a successful guest result is a
failure. A used guest disk is not silently reused as a fresh acceptance run.
Remove only this Compose project and its dedicated guest disk after preserving
its evidence. Existing production controllers, workers, OAuth, approval records
and failed managers are outside the fixture's mutation scope.

Full-host success for both original profiles, plus the explicit Issue #2827
negative scenarios, must be recorded against the exact PR head before claiming
merge readiness. The presence of this driver or a green unit-test run is not that
acceptance result.

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
Only the order of the effective `Mounts` collection is normalized; every complete
mount record, including extra fields and duplicate entries, remains bound.
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

Repeated `EnvironmentFiles` rows retain their emitted order, duplicate
occurrences and `ignore_errors` flags in the route fingerprint. Every referenced
local environment file is independently bound, including explicit absence; an
optional file appearing later changes the observed route. Duplicate singleton
identity or lifecycle properties still refuse.

Unresolved shell/PATH dispatch refuses rather than inheriting an unrelated
classification. Inline `sh`/`dash` accepts only exactly `-c` followed by one
literal absolute command without arguments or shell operators. Combined option
forms, interactive/login shells, overridden dispatcher `argv[0]`, Bash startup
execution and unresolved `env` dispatch are outside the bounded syntax. Common
launchers such as `nice` must retain a supported literal absolute target. Local
shell wrappers and sourced files must use the supported explicit-path subset;
an unchanged wrapper or PATH directory is not evidence that a bare helper's
bytes are unchanged. Unprofiled local Python, Node, Perl and Ruby scripts or
inline evaluation refuse because the wrapper alone does not bind imported
helpers. Direct absolute packaged executables retain the existing trusted-program
boundary, and an explicitly reviewed isolated `-I -B -m` module retains its
separate source binding.

A direct system-manager executable name has a separate measured systemd 255
profile. The installed `systemd-path` utility and running manager versions must
match, and the native compiled search order must be exactly
`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin`. The reader binds each concrete
candidate in that order, including missing earlier candidates, protected ancestor
and symlink identities, and the selected executable bytes. It preserves the
original `argv[0]`, arguments and full systemd command row. The native search
utility, search order and every candidate are read again after source inspection.
Alternate roots, search overrides, filesystem remapping, non-default execution
identity/environment and unsupported argument expansion refuse. Bare shell,
interpreter and launcher names, including aliases resolving to those programs,
do not gain this direct-executable profile. This follows systemd's documented
[compiled direct-command search](https://manpages.ubuntu.com/manpages/noble/man5/systemd.service.5.html#command_lines);
it does not infer a shell's executable search from the observer's environment.

Cron sources retain their actual system-crontab, user-spool, anacron or
periodic/at script role. The reader extracts only the fixed scheduling fields
for that role and checks the command as shell input. Bare PATH helpers, unknown
source roles, unsupported shell selection and percent/stdin syntax refuse.
Source formats cannot be inferred from whether a command resembles an account
name. Full source bytes, identities, symlinks and the whole catalog remain bound.

The finite `ubuntu24-stock-cron-inhibited-v1` profile supports exactly the measured
Ubuntu 24.04 `/etc/crontab`, `/etc/cron.d/e2scrub_all`, and the `apport`,
`apt-compat`, and `dpkg` daily scripts.
Their installed paths, source roles, protected raw bytes and reviewed SHA-256
values must match. Other stock versions and arbitrary opaque scripts retain the
ordinary strict parser. Each supported entry carries a typed inhibition proof
and is always a relevant restarter; its executable dependencies are not silently
classified as unrelated.

This proof requires one real installed, masked, inactive `cron.service`, the
reviewed protected vendor unit and PAM/default/environment files, a bound
canonical cron executable, and absence of the other standard scheduler binaries.
It independently inspects the complete unified cgroup directory inventory and
binds the name and digest of every currently inhibited installed service. All
of their actual service components, descendants and alternate slices must be
empty, together with the canonical service scopes and ordinary PAM user scopes.
This includes a differently named controller whose mask hides an earlier cron
command. Every process/thread membership is also checked, including nonleader
or zombie tasks. Canonical scopes are checked even when a masked service reports
an empty `ControlGroup`. Unsupported masked template, instance, escaped or
reserved service names refuse instead of guessing their kernel group mapping.
Source, mask, controller, boot and kernel scope observations must agree across
fresh reads. A process running a scheduler outside its normal service also
refuses. This initial profile conservatively refuses unrelated masked services
that retain work as well.

A detected remaining task has a typed refusal at the route boundary. Its public
diagnostic contains fixed text and a bounded validated controller-scope identifier
so an independent consumer can distinguish the canonical and differently named
controller cases. Arbitrary exception text, command/environment content and
kernel PID details are not copied into that diagnostic. Unexpected filesystem
and subprocess failures retain the sanitized generic refusal.

Cron files, periodic directories and scheduler executables remain relevance cues
for host systemd, user controllers and local wrappers. An alternative `run-parts`
controller cannot be excluded by approving it as unrelated. Empty current scopes
are present-time evidence only; the observer neither stops tasks nor claims that
a masked controller historically killed every child.

One measured stock data operand is supported separately from executable and
configuration sources. A direct `systemd-fsck` instance can use one literal
`/dev/...` block operand only with the exact protected vendor fragment
`/usr/lib/systemd/system/systemd-fsck@.service`, SHA-256
`43be4420263da4e79a7fe33f6b628449cc56cf99ca30cdc4dedc7f53aff7ea7e`,
no drop-ins or source redirection, no pending reload, and empty execution
environment, working-directory and root-remapping properties. The operand's
original path, resolved block node, device number and metadata identities are
read back together with the canonical installed executable's identity. The
observer never opens or reads the device. The complete command remains in the
route fingerprint. Other uses of the same block path, including `RootImage`,
another command or a source file, retain the ordinary source checks and refuse
when unsupported. This profile neither stops fsck nor changes its boot mounts.

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

The supported Ubuntu fixture installs real cron and also requires the existing
`motd-news.service`/timer, `cloud-init-hotplugd.service`/socket,
`debug-shell.service` and `grub-common.service`. After authorization, fixture setup
stops these controllers and `cron.service` before installing protected persistent
masks, retaining them across all ordinary and quarantine cases. Each appears
explicitly as an inhibited restarter in every approval; its exact mask identity
is preserved during each CLI observation and recorded in the result and a
separate evidence artifact. Stock vendor units and cron source files remain
unchanged.
Cleanup removes only unchanged masks created by this fixture, preserving
preexisting masks and vendor fragments; the disposable VM is then discarded.
Unknown dynamic command sources still refuse until their actual controller is
separately inhibited. Other routes that reach such sources remain subject to
the same ordinary source inspection.

Before any complete approval, the fixture installs one private benign cron entry
and one separately named, masked cron service. Two negatives start the genuine
daemon in the canonical and differently named controlled service cgroups and
wait for that unchanged entry to dispatch a real job. Each retains the job
lifetime with a PIDFD, stops that job, terminates the daemon and requires both
production CLIs to refuse the remaining task while the original mask and source
identities remain unchanged. Other negatives deliberately misclassify a real, fully catalogued
`run-parts` controller and unmask cron. The observer must report the relevant
route/task authority in each case. Cleanup signals only independently retained
fixture task lifetimes and removes only the unchanged fixture source and masks.

Each immutable quarantine command or mount variant also has its own immediately
preceding supported original-profile control. The control uses genuine prepared
source/runtime lineage and separate complete production observe/verify calls;
its runtime, proxy, namespace pin and owner resources are closed before the
invalid variant begins. The resulting evidence pairs the distinct container and
enrollment lifetimes instead of pretending that Docker configuration changed
inside one immutable container. The mutable support-directory permission case
retains its direct same-case positive baseline.

Every production CLI invocation also retains actual preservation receipts outside
its protected paths. The driver writes an exclusive, synchronized before receipt
before launching the process and a final receipt from its completion/error path.
The receipts bind measured file identities and content or symlink-target hashes,
explicit missing paths, the unrelated worker PID/start token, sampling times,
actual CLI lifetime and exit/signal outcome. They contain no protected file bytes.
A failed measurement or unequal comparison fails the fixture; a receipt cannot
replace the separate complete CLI report or turn a refused observation into a
positive result. Each artifact is registered when written so failure output
includes the measurements that were actually completed.

The in-flight producer-death case retains both the paused and final samples.
The deliberate configuration-race case additionally records the real write's
before/after identities and hashes, then compares the unchanged paths across the
whole invocation and all paths across each read-only interval. The intentionally
changed configuration is explicitly not claimed preserved. The producer's final
receipt is written before an independent consumer starts or the fixture restores
its configuration. No earlier run's preservation values are reconstructed.

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

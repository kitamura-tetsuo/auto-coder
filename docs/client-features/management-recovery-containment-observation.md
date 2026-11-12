# Current containment of an ordinary recovery scope

Issue #2840 adds a read-only `ordinary-recovery-v1` observation profile for a
separately approved set of original ordinary management generations and one
exact prelaunch recovery consumer. It supplies current facts to the recovery
issuer. It does not establish original-history completeness, repair an original,
issue a grant, create operation admission, start a child or assert readiness.
The original `ordinary-v1`, `quarantined-original-v1`, fresh replacement and NEW
admission profiles keep their existing contracts.

## Fixed authority and finite selection

The production reader loads only
`/var/lib/auto-coder/approved-recovery-containment-enrollments/<enrollment-id>.json`.
The directory must be canonical, root-owned and mode 0700; the file must be a
canonical, root-owned, single-link mode-0600 regular file. No request or environment
variable can substitute the enrollment root. A separate trusted operator installs
approval; the observer cannot create, repair or replace it.

The exact schema is `approved-management-recovery-containment-v1`. Its fields are
`deployment_id`, `enrollment_id`, `authorization_id`, `profile`, `engine_id`,
`source_identity`, `originals`, `target`, `watchers`, and
`approved_observation_only: true`, together with `schema`. Unknown fields, aliases,
unsorted membership and incompatible versions refuse. The typed definitions and
strict parsers are in `recovery_containment_model.py`.

| Selection | Independently bound information |
| --- | --- |
| Original generation | Generation and instance IDs; exact manager/proxy IDs and images; bootstrap, management and Caddy configuration file identities; complete actual bind roots; current-state selection |
| Started original | Original boot ID; protected original manager/proxy lifetime capture; exact original owner file; live original namespace holder and its actual retained descriptor |
| Never-started original | Existing Docker objects currently in the pristine `created` state; no invented owner, process lifetime, prior record or namespace pin |
| Target | Exact separate manager/image/instance; protected bootstrap and management configuration; complete bind roots; prepared owner inode; distinct loopback published port |
| Watcher | Exact installed systemd unit, actual installed module and protected configuration, and the existing `proxy-watch.lock` that the real watcher takes before tick/reconcile |

There are at most 16 original pairs and 32 watchers. Their complete eventual peer
custody set must also satisfy `3 + 2 × started originals + watchers <= 64`; the
three fixed resources are history, target owner and deployment epoch. Original
pairs, target, owner files and controller files cannot alias. Reusing one proxy
object across multiple enrolled generations is outside this finite profile.

A lineage branch with positive original never-created evidence and no current
Docker object is not fabricated as a containment pair. The issuer joins that
original history to absence from the independently enumerated whole engine. Any
actual branch object remains subject to complete selection and profile checks.
Current `created` state alone supplies no historical never-created certificate.

## Actual executable source and configuration

The observer copies the complete installed `auto_coder` package from the exact
selected manager objects on the local system Docker engine. It reads a bounded
uncompressed archive without executing it. Every Python source filename and byte
sequence contributes to the same length-framed identity as
`original_attempts.source_identity()`. The protected enrollment, current installed
observer and every selected manager must agree on that identity.

Absolute or ambiguous archive paths, duplicate members, links, special files,
missing original-history modules, bytecode and native import extensions refuse.
Package `.pyc`, `.pyo`, `.so` or `.pyd` files cannot substitute different executable
bytes for the observed Python source. The supported container environment sets
`PYTHONDONTWRITEBYTECODE=1` before the first bootstrap import. Existing cached
package bytecode remains unsupported; setting the environment later cannot make
it acceptable. The production Dockerfile embeds build provenance with `python -B`
to avoid introducing package bytecode during that build step.

Effective commands must be the actual ordinary bootstrap entrypoint, the ordinary
fresh entrypoint for an original successor, or the target's explicit
`--recovery-epoch` entrypoint. Originals using fresh initialization keep their
unknown or failed historical outcome; this observation path never requires or
invents a successful fresh outcome. Their historical justification belongs to the
independent lineage producer and issuer join.

The mount reader independently checks the full current Docker inspection,
protected source/configuration bytes, actual bind root device/inode/mode, Python
resolution, runtime command and effective configuration mapping. Interpreter,
package and working-directory shadowing refuse. Original genesis uses the fixed
read-only `/management-original-history` mapping and
`/bootstrap/original-genesis.json` input. The target uses the fixed
`/bootstrap/recovery-epoch.json` input and a separately checked installed runtime;
its version root is read-only at the same absolute host/container path. The issuer
owns the additional tested-source and READY verification.

The target's state and journal roots are separate from every original mutable
root. Its OAuth root remains the exact original OAuth path and object. The new
published endpoint must differ from every original published port, so a target
listener cannot hide an old listener. The actual target listener must be the
local Docker userland proxy for its exact container address and endpoint.

The target's read-only custody mappings are `/management-recovery` from the fixed
host `/var/lib/auto-coder/recovery-epoch-peer`, the exact history lock at
`/management-recovery-history/history.lock`, and the independently selected old
owner files at `/management-recovery-originals/<index>/owner.lock`. Indices follow
the canonical ordered original list. Their actual device/inode and configured
roles must agree. These inputs do not themselves establish acquired custody.

## Complete current controllers and kernel facts

Whole-engine enumeration is unconditional. Every selected object must exist on
the exact local engine. Unselected objects sharing selected images, management
commands, selected namespaces or original bind sources remain management
candidates and refuse. Docker endpoint overrides, foreign engines, changing
inventories and unsupported profiles refuse. Ordinary unrelated objects remain
visible in the complete engine evidence.

The sibling `<enrollment-id>.routes.json` must use
`approved-management-recovery-routes-v1`, bind the exact enrollment hash, and carry
separate explicit complete-route approval. It reuses the existing exhaustive
system/user systemd, activation, cron/at and installed-script readers over the
union of all original and target IDs and actual roots. Every restarter must be
technically inhibited; restart policies must be `no`. Unknown exclusions and
unsupported remote origins cannot be converted into closure by an operator list.
Each real watcher must use its enrolled source/configuration and actual existing
controller lock. The observer changes no unit, restart policy, file or controller.

For each started original, the observer checks the exact captured manager/proxy
lifetimes and mount identities, every remaining cgroup task, absence of captured
escaped tasks, the whole host's task references to the retained original network
namespace, and absence of the old host listener. Stopped Docker state or an old
namespace number is insufficient. The original boot must still match, and a live
holder must still retain the genuine original namespace object.

`RecoveryResources` names a current host holder by boot, PID and process start
token. Its `original_owner_fds`, `controller_fds` and `namespace_fds` name exact
existing descriptors. Original names use the enrolled `generation_id`; controller
names use the exact watcher unit. The observer independently reopens procfs
references, holds observation references, checks protected paths and file objects,
reads kernel flock records, and pins holder lifetimes while it samples.

Every original-owner flock must be SH and independently attributed to that issuer
PID. An inherited parent's SH descriptor is refused. Every real watcher lock must
be held EX by that same issuer. The target separately holds its own owner EX under
its actual prelaunch process. File replacement, released locks, reused descriptor
numbers, holder exit and missing resource membership refuse. No observation claims
that these present fences existed during the historical gap.

## Library and CLI

The production library exposes:

- `load_enrollment(enrollment_id) -> RecoveryEnrollmentObservation`
- `observe(enrollment_id, resources) -> RecoveryContainmentReport`
- `verify(enrollment_id, previous, resources) -> RecoveryContainmentReport`

Missing or unsupported facts raise `RecoveryContainmentUnavailable`. A positive
report has status `observed-current-recovery-containment`, typed complete facts,
a fresh observation ID and boot/process sampling interval, and a deterministic
`evidence_sha256` over stable scope/source/resource/controller/kernel facts.
Diagnostic observer PID and sampling timestamps are outside that stable digest.
`grant_issued`, `history_asserted` and `admission_created` are always false.

```sh
python -m auto_coder.management.recovery_containment observe \
  --enrollment separately-approved-recovery \
  --resources /private/current-issuer-descriptors.json
python -m auto_coder.management.recovery_containment verify \
  --enrollment separately-approved-recovery \
  --resources /private/current-issuer-descriptors.json \
  --previous /private/previous-current-observation.json
```

CLI success exits 0. Unavailable evidence exits 2 with a structured unavailable
result. A resource input only identifies existing descriptors; supplying a number
cannot create a lock, pin or approval. Reports expose hashes and identities rather
than configuration contents. Logger diagnostics may also appear on stdout, so a
machine caller selects the structured JSON result line.

The separate verifier repeats real source, kernel, controller and holder reads.
It rejects changed facts, malformed or authorizing reports, invalid digests,
foreign boots, future samples and samples older than ten minutes. The earlier
read-only sampling command may have exited; its PID is attribution. Actual issuer
and namespace-resource producers must remain alive and independently inspectable.
A saved positive result never replaces a current check or establishes continuity.

## Reproduction and observability

Component tests use the required repository runner:

```sh
CI=true UV_NO_SYNC=1 PYTHONPATH=.:src bash scripts/test.sh --ci-tests-only \
  tests/test_management_recovery_containment.py \
  tests/test_management_recovery_containment_resources.py
```

The parser/source/profile tests are component evidence. The resource tests use real
independent processes, SH/EX flocks, namespace descriptors, process death and file
replacement. Neither suite by itself proves whole-host route or Docker closure.

The native recovery harness uses the existing separately provisioned disposable
KVM/QEMU VM authorization, initial host namespaces, PID 1 systemd and the local
system Docker engine. `scripts/recovery_epoch_containment_fixture.py` captures
live original generations before stopping them, retains genuine namespace FDs,
runs and inhibits actual installed watchers, and constructs the separately
approved finite fixture scope. It imports production observation readers and
creates no substitute successful evidence. The production observer does not
import this fixture helper. Native runs must include genuine lineage and failed
branches, the prelaunch target, actual unrelated workers and preserved original
records; a component-only pass cannot be reported as native acceptance.

Observation logging uses Loguru's configured file and stdout sinks. Its explicit
fields are `observation_only=True`, `recovery_grant=False` and
`admission_created=False`. No operation journal admission, dispatch, recovery
success or readiness event is emitted. The native harness verifies original
journal/state and unrelated worker lifetime/request fingerprints across the
production entrypoints. Diagnostic sampling output is not an operational success
row in the dashboard; the shared dashboard observability documentation records
this distinction.

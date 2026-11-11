# Read-only current containment prerequisite observations

`python -m auto_coder.management.containment_observation` provides a bounded
**unavailable prerequisite report**, not a successful closed-profile observation.
Issue #2827 is not fully implemented by this adapter. Neither selector currently
has an accepted production profile. The existing `fresh_host` capture/claim/guard,
replacement admission, retained journal and original owner boundaries are unchanged.
No recovery launch or admission from #2824/#2825 is implemented here.

The explicitly versioned selectors are `ordinary-v1` and
`quarantined-original-v1`. They identify requested review targets, not reviewed
original source evidence. The adapter must not infer that today's installed
bootstrap bytes were the original deployed bytes. The ordinary source currently
in this checkout takes `.supervisor-owner.lock` exclusively in
`ManagementBootstrapSupervisor.run` before calling `_run_owned`; that source inspection
does not bind an original deployment to that implementation or owner inode.
The quarantine selector additionally reports the absent exact bootstrap file
mounts, `no-new-privileges` setting, and support-mount purpose, source, destination,
read-only and execution binding. It does not accept a quarantine configuration as
a replacement profile or loosen the existing replacement checks.

## Interface and available facts

Create a scope selection JSON with exactly these keys:

```json
{
  "deployment_id": "proposed-deployment",
  "enrollment_id": "proposed-enrollment",
  "authorization_id": "proposed-authorization",
  "profile": "ordinary-v1",
  "management_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "proxy_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "files": []
}
```

IDs here select a proposed scope only. `files` can select up to 32 sorted unique
absolute file paths, for example the installed controller source and effective
configuration. Canonical regular files of at most 1 MiB are read without following
symlinks. The report includes their device, inode, size, change/modification times
and SHA-256, never file contents. It checks their identities again after Docker
observation. File hashes are observations, not independent enrollment approval or
proof that the configuration is immutable. Files, stores and owner locks are not
created, locked, repaired or modified by these reads.

```sh
python -m auto_coder.management.containment_observation observe \
  --scope /private/proposed-scope.json > /private/prerequisites.json
python -m auto_coder.management.containment_observation verify \
  --scope /private/proposed-scope.json --previous /private/prerequisites.json
```

Both commands return exit status **2**, even if every available sub-observation
matches. Output is JSON. `current-containment-prerequisites-v4` includes the full
proposed scope and its hash, a new observation UUID, current boot ID, observer PID
and process-start token, monotonic observation interval, observed file identities,
available Docker identities and explicit unavailable authorities/reasons.
Malformed inputs also return 2 with an unavailable diagnostic. Duplicate JSON
keys, arbitrary profile names, relative paths and ambiguous container IDs refuse.

The Docker reader only uses `context inspect`, `info`, unfiltered `container ls`
and container `inspect`.
It requires the local `/var/run/docker.sock` context and refuses Docker endpoint
overrides. Complete inspection hashes bind selected container IDs to the engine,
image, effective command, mounts, security options, restart configuration and
current Docker lifetime without exposing environment/configuration contents.
Two independent complete engine reads must agree. This inventory includes every
listed container; it does **not** establish deployment enrollment, close launch
routes or prove task absence.

The verifier independently reads the selected current files, Docker facts and
selected Linux process facts; it never accepts a saved success assertion. It
compares scope, boot, file, Docker, process and resource-limit observations,
including all prior explicit unavailable reasons. Removing or replacing missing
prerequisite reasons in a saved report is detected even though the reobserved
result always remains unavailable. Its fresh process and observation identities
differ from the producer. Agreement does not remove any missing prerequisite.
A saved object remains unavailable after the producer exits, and is never a
launch grant, historical completeness claim,
continuous exclusion, or reservation of a listener/namespace.

## Missing authoritative prerequisites

An accepted production observer still requires initial-namespace Linux host root,
unrestricted host procfs/cgroup visibility and local Docker authority. More
importantly, it requires original authoritative deployment provenance or a
separately approved present-time enrollment and independent original installed
source/controller/route authority. Merely naming an enrollment or supplying
operator lists is insufficient. No enrollment or controller mutation is performed here.

### Protected enrollment component (v4)

The v4 report optionally includes `approved_enrollment`, a bounded component
readback from the fixed trusted-local path
`/var/lib/auto-coder/approved-containment-enrollments/<enrollment-id>.json`.
The observer never writes to that path. Initial-namespace host root (matching
PID, user, mount and network namespaces to host PID 1, with unrestricted procfs)
and an existing private
root-owned directory (mode 0700), private regular file (mode 0600), canonical
paths, exact deployment IDs, original image IDs and independently reopened
protected original source/configuration and prior-evidence file identities are
required. The observer reopens the record and originals after reading them.
Absent, mismatched, mutable and uninspectable records contribute
`approved-enrollment-component` refusal reasons. Unit-level fixtures use a
separate explicitly injected private directory and their own UID, never the
production CLI path.

An installed record only establishes that a protected *candidate approval
component* can be inspected. File hashes and root ownership do **not** establish
that bytes were the original running program, that an enrollment was separately
approved, that its scope covers all ordinary restart/launch routes, or that
Docker/kernel/owner-fence containment holds. The report therefore continues
to carry the independent `deployment-enrollment`, `original-source-profile`,
`ordinary-route-closure` and `kernel-containment` unavailable reasons and
always returns status 2. There is no accepted profile, authority to launch or
full-scope positive fixture yet.

Run the new bounded protected-record regressions with:

```sh
bash scripts/test.sh --ci-tests-only tests/test_management_containment_enrollment.py
```

These tests use owner-private disposable files and real file identities, but
the test-created approval record and prior facts are deliberately not accepted
as original deployment authority or positive controller containment.

An optional host-only integration probe exercises the production `observe` and
separate-process `verify` entrypoints against a disposable *real* Docker manager
and proxy with a root-private test-only approval record. On an explicitly
authorized Linux/Docker test host with the preexisting image
`auto-coder:management-local-abc65544`, run as initial-namespace host root:

```sh
python scripts/test_management_containment_enrollment_host.py --confirm-test-only-fixture
```

This probe does not pull images. It creates uniquely named test containers and
only its own files under `/var/lib/auto-coder/containment-readback-fixtures` and
the fixed approval directory, and cleans up those resources after completion or
failure. It checks that a protected test approval is actually read in both
entrypoint processes, that the full containment verdict remains unavailable,
and that a post-observation original-file substitution is rejected by the
separate verifier. It does **not** exercise or alter the original production
controllers, nor establish original production provenance or a full positive
closed profile. The operator must ensure that the host, the fixed approval
directory, the test image and temporary Docker/localhost-port effects are
separately authorized before running this probe.

The installed route inventory must independently cover the actual watcher and
explicit reconciliation, services/timers, socket activation, Docker restart
policies, configured local and remote scripts, repair/recreate routes, and
alternative roots. This adapter cannot establish that inventory and always
reports it missing. Unknown, omitted, enabled, uninspectable or mutable alternative
routes cannot be declared outside the deployment. It does not impose a new threat
model on trusted Docker, Git, worktrees or later administrator actions.

An accepted observer also needs the exact original manager/proxy/task and boot
lifetimes, all cgroup tasks including nonleaders, currently pinned namespace
objects, current occupancy and endpoint listeners, and exact original owner-file
fencing evidence. No such pins or fences are held by this report. Saved inode
strings, stopped Docker state and inaccessible tasks cannot prove absence or
exclude escaped tasks, namespace reuse, zombies with uncertain lifetimes, or
observation races. Existing conservative host observation helpers are unchanged.
All temporary read descriptors are closed before return, including refusals.

## Validation boundary and remaining Issue coverage

Run `bash scripts/test.sh --ci-tests-only
tests/test_management_containment_observation.py -m 'not supervisor_integration'`.
These regressions invoke the
actual checked-out module in separate real Linux processes, fingerprint actual
bootstrap source, detect changed controller source and alternate selected roots,
reject cross-deployment/enrollment/authorization/profile replay, preserve selected
retained-file contents and identities, and independently reobserve after producer
exit. A deterministic unit interleaving supplements these tests for file mutation;
it is not production authority or an acceptance substitute.

These tests establish part of REQ-401/404/405/406's refusal and interface behavior.
They do not establish complete REQ-401/402/403/404/406 compliance. All AS-T01–AS-T05
production-host acceptance scenarios remain unpassed: positive exact-source
ordinary/quarantine containment, real watcher/service/timer/socket/Docker route
inhibition and omission detection, unselected alternate roots, escaped/nonleader
tasks, namespace reuse/occupancy and conflicting listeners, exact mount/security
substitution, controller races, and original-store/worker/OAuth preservation across
real host attempts require the missing deployment authority, exact original
profiles and isolated root/Docker/controller harness. The implementation sandbox
denies Docker socket access, TCP socket creation and Unix socket binding, and
runs as UID 1000, so those fixtures cannot run here. The bounded validation run
also exercises the unchanged `tests/test_management_fresh_initialization.py`:
its four socket-dependent cases fail with `PermissionError: Operation not
permitted` in this sandbox. They are not skipped, weakened or counted as passing.
No successful synthetic-profile test is reported as acceptance.

This path creates no operation admission/dispatch, worker effect, OAuth change,
recovery-success event, or Issue/PR dashboard trace. Its unavailable JSON is local
diagnostic output only. Later integration of accepted evidence into #2824 owns
the corresponding production trace-to-view obligations.

## Complete current Docker inventory sub-observation

The `current-containment-prerequisites-v4` report replaces v1/v2/v3. Older saved reports
are refused; they did not bind the complete engine inventory. `docker` now includes
**every** container returned by unfiltered `docker container ls --all --quiet
--no-trunc`, sorted by full ID, rather than only the proposed manager/proxy pair.
Both selected objects must occur in that inventory. Each object records the engine,
container and image IDs, complete inspection digest, mount digest, explicit current
restart policy and retry limit, running/status/PID, Docker start/finish timestamps
and restart count. Docker timestamps/PIDs are Docker observations, not independently
correlated kernel lifetimes. Raw configuration, environment and mount contents are
not emitted.

Enumeration occurs before and after inspection; the entire read is then repeated.
Engine/context changes, additions/removals, differing inspections, missing selected
objects, unsupported/missing restart policies or lifetime fields, contradictory
Docker running/PID state, duplicate/short IDs, inspection failure and inventories
over 512 containers refuse the Docker sub-observation. For a Docker-running
object whose selected positive PID is independently unreadable, exited or lacks
a current Linux boot/start lifetime, the adapter emits a container-specific
`docker-process:<full-id>` unavailable reason; the separate consumer reobserves
and compares that reason. A consistent selected PID remains only a partial
observation, not proof that other escaped tasks or restarters are absent.
The bound keeps saved reports within the verifier's file-size limit. The verifier
repeats all these reads, including objects omitted from the proposed scope,
and compares the full inventory. Any configured `always`,
`unless-stopped` or `on-failure` policy produces a container-specific
`docker-restart:<full-id>` unavailable reason, even for a stopped container.
A `no` policy is a positive observation of Docker's current configuration only;
it does not establish inhibition by a watcher, service, timer, socket, script or
repair route. Unselected objects remain of unresolved deployment membership;
labels and names never exclude them. Inventory/digest agreement neither proves
immutable source/configuration nor closes alternative roots.

Every command still returns status 2. Both exact original
profiles, independent enrollment and complete route/kernel authority remain
unavailable. No original histories have been recovered.

Run the bounded ordinary regressions with:

```sh
bash scripts/test.sh --ci-tests-only tests/test_management_containment_observation.py -m 'not supervisor_integration'
```

The deterministic Docker protocol tests are explicitly unit tests, supplementing
the real Linux separate-process tests; they are not runtime acceptance evidence.
The separate `test_real_docker_inventory_and_separate_consumer` fixture is selected
and required by the Supervisor `isolated-containers` CI profile. To run it locally
on an authorized disposable Docker test host with an already prepared image:

```sh
AUTOCODER_CONTAINMENT_TEST_IMAGE=python:3.12-slim bash scripts/test.sh --ci-tests-only tests/test_management_containment_observation.py::test_real_docker_inventory_and_separate_consumer
```

It also accepts the CI `AUTOCODER_SUPERVISOR_IMAGE`. It never pulls an image.
It creates only randomly named `ac-containment-test-*` containers with networking
disabled, never starts them, changes a restart policy only on one such container,
and removes only its own objects. It mounts a disposable copy of current bootstrap
source and fingerprints a retained fixture file. It drives the real producer and
separate verifier after producer exit, checks unchanged inventory readback, then
adds an unselected object with a configured restarter and substitutes selected
source bytes. This validates only the Docker inventory and file sub-observations;
it does not run the exact original controller or qualify AS-T01–AS-T05 as passed.

In the prior v2 revision session, 30 ordinary tests passed. The real Docker case
failed at its first `docker info` call with socket permission denied, before
creating any fixture object; it was not skipped. Host UID was 1000. Thus positive
Docker/controller acceptance remains unverified in this environment, and Issue
#2827/PR #2829 remain incomplete. No production or beta deployment is performed.


## Current Linux process observation component (bounded REQ-402/REQ-406 work)

The v4 prerequisite report retains `processes`: current `/proc` observations for
positive PIDs in the Docker inventory. A stopped object's PID 0 supplies no task
absence evidence. Docker PIDs only select proposed current processes; this does
not correlate them to original manager/proxy lifetimes or discover escaped tasks.
The full entrypoint still returns unavailable and exit status 2.

The same read-only component can be exercised without Docker for explicitly
selected thread-group leaders, independently of deployment authorization:

```sh
.venv/bin/python -m auto_coder.management.proc_observation observe --pid 1234 > .agent-tmp/process-facts.json
.venv/bin/python -m auto_coder.management.proc_observation verify --pid 1234 --previous .agent-tmp/process-facts.json
```

Both commands always return status 2 and `status: unavailable` under the separate
`current-proc-facts-v1` schema. Supply the consumer's intended PIDs explicitly;
saved objects never choose its scope. The consumer reopens procfs and pidfds and
compares fresh complete process facts, including boot ID and task start tokens,
without trusting signatures, saved success claims or numeric PID stability.
The full v4 verifier likewise compares newly read `processes`; v1/v2/v3 reports are
refused as unsupported rather than upgraded into authority.

Each selected process records every currently enumerated thread's TID, start
token, state, cgroup membership, current namespace links and socket FD inodes.
Network tables record TCP/TCP6 listening sockets, bound UDP/UDP6 sockets and Unix
listening socket inodes in that process's current network namespace. These tables
include other processes' listeners in the same namespace; an inode in a task's
FD list supplies a current ownership correlation, not exclusivity. Unix paths
are omitted. Different network namespaces among threads refuse because their
individual tables cannot be independently inspected by this reader. Namespace
strings are current facts only, never historical pins or namespace-absence proof.

A pidfd brackets two task/socket reads; observed lifetime, task-set, namespace,
cgroup, FD/socket or boot changes refuse. Zombies/dead tasks preserve their
observed lifetime/state and explicitly refuse instead of becoming absence.
Missing, inaccessible, oversized, malformed and raced reads retain explicit
unavailable diagnostics. Reads are bounded to 1 MiB per file, 4096 tasks, 8192
FDs per task and 32 explicitly selected processes in the standalone interface.
All descriptors are closed on return, including refusal. Procfs sampling is not
atomic and cannot rule out a change and restoration between samples.

This component requires Linux procfs visibility and pidfd support for inspected
live tasks; missing privileges or inspectability never become an empty successful
scan. It neither validates initial host namespace/root authority nor scans the
whole host, enumerates all cgroup tasks, retains namespace pins, reserves endpoints,
closes ordinary launch routes, or establishes approved installation/enrollment.
Those missing authorities remain full-observation blockers. Current namespace
occupancy by selected tasks can be observed, but complete deployment-wide absence
cannot be inferred from selected processes, even when all subreads agree.

Run the bounded component regressions with:

```sh
.venv/bin/python -m pytest tests/test_management_proc_observation.py tests/test_management_containment_observation.py -m 'not supervisor_integration'
```

These include real subprocesses with nonleader threads, zombies synchronized by
`waitid(WNOWAIT)`, real thread creation between reads, producer/target exit and
separate consumers, plus bounded/malformed OS table fixtures and explicit unit
fault interleavings. The real disposable Unix-listener regression requires bind
permission: sandbox EPERM is a failing prerequisite, never a skip or fake pass.
It must also run on an authorized disposable host. None of these component tests
qualifies AS-T01–AS-T05 or exact original-source/controller acceptance as passed.
No operation admission, dispatch, recovery-success or dashboard event is created.


V3 sandbox validation ran both test files excluding the Docker integration marker:
43 tests passed and the disposable Unix-listener test failed at `bind()` with
`PermissionError: [Errno 1] Operation not permitted`. After fixture synchronization
was tightened, the 43 runnable cases passed again with that already demonstrated
bind-blocked case excluded from the bounded rerun. The listener assertions remain
intact. Execute the blocked test on an authorized disposable Linux host with:

```sh
.venv/bin/python -m pytest tests/test_management_proc_observation.py::test_real_unix_listener_inode_is_observed_and_consumer_detects_release -q
```

Black, isort, flake8, package-wide mypy (330 source files), and `git diff --check`
passed. Real Docker/controller/profile acceptance was not executed in this v3
sandbox session, and no AS-T01–AS-T05 success or full Issue completion is claimed.

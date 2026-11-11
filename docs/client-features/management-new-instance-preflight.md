# NEW-instance origin and isolated first launch (DRAFT)

Issue #2672's Objective remains: “Allow an operator to initialize a new management
bootstrap while retaining existing OAuth state.” The read-only assessment and
protected one-use origin record remain distinct from first-launch authorization.
This draft now also has an explicit, bounded one-shot start for a **genuinely NEW,
never-started, isolated Docker container**, with actual owner/guard claims,
native store initialization, one physical child and authenticated exact-child
readiness observation. It is not a generic migration of an existing manager,
an authorization to deploy, or proof that arbitrary host startup controllers
are excluded. The full REQ-001–005 acceptance and production release remain open.

A trusted local operator can invoke the existing host boundary:

```sh
python -B -m auto_coder.management.fresh_host preflight \
  --request-file /private/new-instance-request.json
```

This command accepts only an explicitly supplied effective-UID-owned 0600 regular
JSON file, with the following exact schema (no duplicate or additional keys):

```json
{
  "schema_version": 1,
  "purpose": "NEW",
  "request_id": "00000000-0000-4000-8000-000000000001",
  "new_instance": "operator-selected-new-identity",
  "runtime_reference": "operator-selected-runtime",
  "state_root": "/private/new-state",
  "operation_root": "/private/new-operations",
  "guard_root": "/private/new-guard",
  "oauth_root": "/private/existing-oauth",
  "bootstrap_config": "/private/bootstrap.json",
  "management_config": "/private/management.yaml",
  "executable": "/private/runtime/python",
  "application_root": "/private/application"
}
```

The operator must separately prepare empty state, operation and guard directories
owned by the acting effective UID with mode 0700. All four roots must exist,
be canonical absolute non-symlink directories, be distinct and non-overlapping,
and have distinct observed device/inode identities. The existing OAuth root is
observed only as directory metadata: its contents, ownership and access are not
changed, and its database and grants are never opened. These observations do
not establish that retained grants are usable or that an instance name is unused
elsewhere. This is a trusted local file boundary, never an OAuth authorization or
management MCP action. It does not assume a hostile coding agent or create a
new container security boundary.

Request and configuration files require mode 0600 and effective-UID ownership;
inputs cannot overlap any root. All path components are opened without following
symlinks. Input files must be regular with one link; special files and application
symlinks refuse. The executable must have an executable mode bit. Assessment
limits are 64 KiB for the request, 64 MiB per other file, and 4096 entries/64 levels
for the nonempty application closure. Unsupported profiles refuse conservatively.

The JSON result contains `status: assessed-only`, `launch_authorized: false`,
the explicit request, original request/configuration/executable/application byte
hashes, file identities and root identities, and a digest binding those
observations. Hashes preserve the distinction between semantically equivalent
JSON and the original bytes. Configuration semantics and actual runtime selection
are **not** verified: names and runtime labels remain operator intent. No
credentials or file contents are returned. The in-process
`revalidate_new_instance` API freshly repeats the assessment and refuses a change,
including original whitespace, inode, permissions, content or new root entries.
This is not the required immediately-before-launch check.

Success exits zero only for a completed assessment; refusal exits one and prints
no assessment. Existing, partial, inaccessible, symlinked, overlapping or
insufficiently protected targets refuse without replacing anything. The command
never writes intent, claims, stores or readiness records, never acquires launch
fences, and never executes the selected application. It does not connect to
Docker, the endpoint, a host guard or any service. Reads can update filesystem
access times; preservation here means original bytes, identities and access
permissions, not a promise of unchanged access-time metadata. The operator must
keep sources stable; observations are bounded snapshots, not reservations
against later changes, bind mounts or a malicious same-UID writer.

## One-use protected origin recording — not a launch claim

An operator can separately record the assessed NEW origin and independently
inspect it in another process through the **existing trusted-local host
boundary**:

```sh
python -B -m auto_coder.management.fresh_host begin \
  --request-file /private/new-instance-request.json
python -B -m auto_coder.management.fresh_host inspect-begin \
  --request-file /private/new-instance-request.json
```

The begin producer re-runs the strict read-only assessment, verifies empty
private roots and writes exactly one `new-instance-origin.json` into the new
0700 guard root using owner-only 0600, O_EXCL, file fsync and directory fsync.
The record retains exact assessed identities/hashes, request provenance and
actual Linux producer PID/start-token/boot lifetime. Any interrupted/partial
record prevents replay and is never unlinked or repaired automatically. A
second process reopens the original request, source/configuration/application
closure, protected roots and original guard record; it refuses conflicting
records, changed bytes, altered permissions or an inconsistent digest.
The independent verifier also refuses malformed original Linux producer
PID/start-token/boot-ID facts in otherwise canonically encoded records. The
producer reopens the published guard path and checks **both filesystem device
and inode** against the originally assessed root; a coincident inode number on
another mounted device is not sufficient identity. This final check is a bounded
observation, not continuous host fencing. It
checks retained fact structure, not whether that past PID is currently live;
neither result establishes current host ownership or launch authority.

For an eventual first-start consumer, the same independent origin reader accepts
explicitly named additional guard, state and operation entries as an internal API,
while preserving the original directory device/inode/UID/mode and rechecking
the original request, inputs, and application bytes. It rejects unexpected
entries. Those permitted new entries must be verified independently by their
own original producer; an allowlisted name here is **never** proof of its
content, a consumed claim, a host grant or a successful launch. The public
`inspect-begin` command retains the stricter no-additions behavior.

An accepted begin reports `status=origin-recorded-only` and
`launch_authorized=false`. This is a **prospective fact of an explicitly
requested original intent**, not evidence that original owner/guard fences,
installed runtime provenance, current OAuth read authority, absence of a prior
listener, durable first-launch admission or authenticated readiness were
verified. It intentionally does **not** create operational stores, cross a
first-launch grant, start processes, access OAuth contents or mutate a Docker
controller. No consumer may treat a recorded origin as a completed NEW
initialization or as old-history continuity.

### Independent selected-profile check (non-authorizing)

After `begin`, the operator can use another host process to verify a **bounded
source-script management profile** against the retained origin:

```sh
python -B -m auto_coder.management.fresh_host inspect-profile \
  --request-file /private/new-instance-request.json
```

This second consumer checks the original request and record, then uses the
production bootstrap and management configuration parsers to bind the selected
new instance, distinct roots, initial runtime reference, executable digest,
application source root, exact `management serve --config` command and private
loopback `/mcp` endpoint. It requires the management configuration to select the
same bootstrap, OAuth root, operation root and an enabled read scope. It also
verifies an already-existing retained OAuth database in immutable read-only
mode against the expected schema, issuer, resource and project. It does not
initialize that database, migrate it, open token material for output, or
reconfigure permissions. Missing or malformed OAuth data and unsupported
selected runtime profiles refuse without writing or launching anything. A
second origin inspection after these checks detects changed original inputs.

The selected bootstrap configuration explicitly requires
`"new_instance_initialization": true`, its protected `fresh_guard_path`, and
**no** `fresh_prior_lock_path`. A genuinely NEW profile must not invent an old
owner-lock path simply to satisfy the prior-cutover configuration schema.
The normal Supervisor entrypoint, including its legacy `--fresh-initialization`
switch, refuses this selected NEW profile before creating an owner lock, ledger,
or log file. The separate `--new-initialization --request-file` PID-1 entrypoint
is required, and is accepted only after the trusted local host has selected the
exact Docker first-start scope. The selector alone never enables a process start.

The bounded profile requires the selected runtime's `source_root` to name the
same immutable application closure supplied in the origin request. Installed
runtime profiles, archived exclusions and container-only runtime bindings are
not supported by this bounded mode. The result is `profile-verified-only` with
`launch_authorized=false`: neither configuration parsing nor a valid retained
OAuth database proves an active grant, current owner, host-guard challenge,
first-launch claim, authenticated readback or any readiness. The distinct
Docker start consumer described below performs the actual admission.

### Transient NEW admission serialization (non-authorizing)

Once an original origin and selected management/OAuth profile have been
verified, an operator can probe an exclusive admission fence on the exact
original, owner-only guard directory:

```sh
python -B -m auto_coder.management.fresh_host probe-admission-fence \
  --request-file /private/new-instance-request.json
```

The production `hold_new_admission_fence` context manager obtains a real Linux
nonblocking exclusive `flock` on the directory descriptor, then re-verifies
the original source files, application closure, configuration, OAuth schema
and guard identity **while retaining the lock**. It also rechecks retained
source and guard identity before returning from the held context, refusing a
guard path detached or configuration changed during the hold. This final
bounded observation does not prevent changes after the check or confer
continuous custody/launch authority. Another process attempting
the same guard concurrently is refused by the kernel. The CLI releases the
lock before it exits and reports `admission-fence-observed-only` with
`launch_authorized=false`; its receipt cannot be reused as launch authority.
Killing a holder releases only this transient kernel lock, not a consumed
first-launch claim. Original state/operation roots stay empty and retained
OAuth is not modified.

The standalone probe is only a local serialization observation.
It is **not** the supervisor's `.supervisor-owner.lock`, and does not exclude
any earlier manager or startup path unless that path is actually constrained
by independently verified host authority. Issue #2672's no-prior-lock profile
requires affirmative trusted host proof that no prior configured owner,
child or listener exists **and** continuous enforceable exclusion of prior
startup through first launch. A missing lock, scan or operator assertion
alone does not prove this. The one-shot Docker controller additionally holds
this very guard inode lock across its actual start and first-child observation;
the probe itself still never authorizes a launch.

### Isolated Docker CREATED-state snapshot (non-authorizing)

For an operator-approved dedicated test container, the trusted host can read
Docker's independently maintained **never-started** state:

```sh
python -B -m auto_coder.management.fresh_host inspect-new-container \
  --request-file /private/new-instance-request.json \
  --container-id FULL_64_CHARACTER_DOCKER_ID \
  --image-id sha256:FULL_64_CHARACTER_IMAGE_DIGEST
```

The command rechecks original NEW intent, selected management configuration
and retained OAuth schema, then inspects the exact immutable Docker container
and image identities twice against the same engine ID. It requires Docker's
`created` state, zero PID/restarts and never-started timestamps, an isolated
`--network none` network namespace, private PID namespace, no automatic Docker
restart policy, a read-only container root, non-root effective UID/GID,
disabled added capabilities, no privileged mode, device mappings, published
ports or Docker-socket mount. All original request, configuration, executable,
application and new/retained state paths must be explicitly mounted at their
original canonical host paths with the declared read/write access. Extra or
missing mounts refuse. No Docker create/start/stop or state write occurs.

A valid observation is `docker-created-snapshot-only` and explicitly has
`launch_authorized=false`. It proves only that this exact Docker engine was
observed reporting an unstarted, isolated container at the observation time.
It cannot by itself reserve Docker start, establish that some other service
will never start, prove a continuously held prior-startup exclusion, show that
the recorded command is the actually executed runtime, or authorize a first
management child launch. The separate command below is the only supported
first-start consumer for this specific Docker profile. Tests use disposable
synthetic OAuth and containers only; they do not attach real production OAuth
or affect beta/release.

### Explicit one-shot start for an isolated genuinely NEW container

The operator must prepare an image whose immutable PID-1 command is exactly:

```text
python3 -B -m auto_coder.management.bootstrap_supervisor --config /private/bootstrap.json --new-initialization --request-file /private/new-instance-request.json
```

The actual Docker `Config.Entrypoint` and `Config.Cmd` must contain these
separately, with original absolute paths. The original bootstrap config must
select `new_instance_initialization=true`, an initial source-script runtime,
the precise `management serve --config` command and the same retained OAuth
and private roots. The Docker container must be *created but never started*;
the read-only Docker inspection above must succeed before the first start.

The non-root trusted host operator invokes:

```sh
python -B -m auto_coder.management.fresh_host start-new-container \
  --request-file /private/new-instance-request.json \
  --container-id FULL_64_CHARACTER_DOCKER_ID \
  --image-id sha256:FULL_64_CHARACTER_IMAGE_DIGEST
```

This command is **effectful**: it acquires a nonblocking kernel `flock` on the
original guard directory, re-verifies the selected original and exact Docker
CREATED state, binds a private Unix host guard, and durably writes a one-use
`new-docker-first-start.json` using O_EXCL, mode 0600 and fsync **before**
making one Docker Engine start request. The host requires a local Unix Engine
socket, independently matches the Engine API identity against the recorded
Docker CLI identity, and accepts only the Engine's **HTTP 204** original-start
result. In particular, Docker CLI returns success for an already-running
container, but the Engine API returns **304**, which now refuses without
mistaking another controller's start for this operator's start. Remote Docker
contexts and unverifiable socket/Engine identity are refused. Failure, process
crash, ambiguous observation or interruption leaves the claim in place and
cannot issue another first start. The Engine start request is the only Docker
lifecycle effect initiated by this command.

The host also compares the original Docker CREATED container's immutable
identity and selected startup/containment fields, including its no-restart
policy, after its durable claim and again during the PID-1 guard handshake.
An out-of-band Docker `update --restart=always` during admission is a
refusal, not a newly authorized restart source; restoring `--restart=no`
after a consumed claim cannot reauthorize first launch.

After Docker starts, PID 1 must independently verify the original source,
selected runtime and OAuth schema, keep a single exclusive owner lock, publish
the native current-incarnation ledger and obtain a fresh host challenge bound
to the original request digest, exact container, owner incarnation and peer
UID/PID. The host independently rechecks Docker PID 1, its kernel owner flock,
and the matching native ledger before releasing that challenge. PID 1 consumes
its own separate one-use `new-first-launch-claimed.json` and initializes the
new child, operation, self-update handoff and recovery stores under owner-only
permissions. Only after source and endpoint revalidation does it physically
start the selected child exactly once and write `new-first-launched.json`.

The successful *host command* only reports
`first-child-observed-awaiting-authenticated-readback` and
`launch_authorized=false`; this is **not** authenticated readiness. The
actual child must answer the configured unauthenticated Bearer challenge, and
a currently valid retained OAuth read-grant client must invoke
`auto_coder_whoami` with the fresh `observation_id`. The existing management
readiness reader independently verifies the live PID/start token, generation,
owner incarnation, nonce, source/config receipts, grant/client/resource/project,
read scope and time window. Only then does the protected one-use
`new-first-outcome.json` record `succeeded`. An exit, timeout or unverifiable
proof records or remains `unknown`; neither state permits a second first start.

All original files and consumed claims must be preserved. No cleanup, manual
Docker restart, fresh grant creation, legacy `--fresh-initialization` fallback,
automatic repair or rollback is authorized by these commands. A new attempt
needs separately authorized reconciliation and independent evidence; deleting
one of these records is not a reconciliation. A host root or Docker controller
outside the exact isolated container may still be governed by different
startup sources: this mode does **not** establish exclusion of unknown or
unmanaged host-level managers. No production credentials or services have been
mounted into the isolated acceptance tests.

Independent native Linux subprocess tests hold two writers immediately before
the original O_EXCL to establish one winner, and SIGKILL a producer immediately
after the actual open to establish a non-reusable partial origin. The isolated
Docker E2E separately establishes one genuine first start, exact-child
authenticated readback, actual kernel admission contention between independent
host processes, consumed-claim replay refusal and a SIGKILL boundary just
after the host's durable Docker-start claim. These tests are affirmative
evidence **only for the disposable isolated NEW profile**.

### Optional host-loopback-only Docker network profile

The original `--network none` first-start profile remains the default. Its
readback is reachable from inside the container, not from the host. A distinct
operator-selected NEW bootstrap can opt in **before original `begin`** by
including the following two fields in the original protected bootstrap JSON:

```json
{
  "new_docker_network": "ac-new-unique-disposable-network",
  "new_docker_host_port": 43981
}
```

The named network must be dedicated to this container and created as a local,
non-attachable, IPv6-disabled Docker bridge with IP masquerading disabled.
The Docker Engine server must identify as **28.0.0 or newer**; older or
unverifiable releases are refused because older Docker releases could expose
localhost-published ports to other hosts on the same L2 segment. The operator must
select a free host port, provide `--network` with the exact selected name and
`--publish 127.0.0.1:43981:18881` when creating the never-started container.
The internal port in this example (`18881`) is defined by the selected
`listener_url`, not hardcoded by the host inspector. Docker's `--internal`
bridge mode is **not** an equivalent profile: it disables the necessary host
port forwarding on the tested engine.

The matching management YAML must use `bind_host: 0.0.0.0` to accept packets
inside the dedicated container network, but its `public_origin` must be
`http://127.0.0.1:43981`. The original bootstrap `listener_url` remains the
internal `http://127.0.0.1:18881/mcp`; both identifiers and ports are bound
by the original protected file hashes. A wildcard HTTP management bind is
refused outside this exact NEW bootstrap configuration. No production OAuth
grant or privileged host socket is mounted as part of this test profile.

The independent Docker CREATED observation rejects public/all-interface
publishing, changed host ports, additional published ports, an unrelated or
replaced network, IPv6, IP masquerading, other network members, an already bound
host-loopback listener and unsupported namespace policies. The host start
guard additionally compares the **running**
container's actual network ID, endpoint and host port mapping with the durable
original and current Docker network identity before allowing first-child
admission. A host `127.0.0.1` client must receive a real Bearer challenge and
then issue `auto_coder_whoami` with the synthetic retained read grant; this is
the only positive loopback-network acceptance oracle. It does not establish
public-domain, cross-host or existing-installation connectivity.
An exact loopback host publish is **not sufficient evidence of network isolation**
when the Docker daemon enables direct routing of published container ports,
or when host firewall/routing policy is not trustworthy. This bounded profile
does not claim to prove those host-global network settings; do not consider it
an approved production/public deployment without independent host-network
policy verification and the complete prior-startup exclusion contract.

Native Docker refusal tests additionally exercise a second real OS principal
(effective UID 65534) attempting the original host first-start command against
the unchanged 0700 private roots, a running listener in a second container on
the **selected dedicated bridge**, and a live sibling process joining that
bridge **after the durable host start claim** while the first host is suspended
at an observable barrier. Each refuses before any management-child invocation;
the original OAuth database and origin remain unchanged. The mid-admission
case retains its consumed claim, and removing the competing process does not
permit replay. These facts show revalidation for tested process races, not a
general guarantee that every unrelated host service or Docker API controller
is continuously excluded.

A separate, narrower race regression pauses the host **after its final
CREATED-state check but before the Engine start request** and has another
independent client start the exact same container. The original host must
receive Engine HTTP 304, refuse authorization and preserve the consumed claim
without ever starting the selected management child. This verifies that the
host can distinguish its own successful Engine state transition from a
successful-but-no-op Docker CLI invocation.
Another regression changes the real Docker restart policy from `no` to
`always` after the durable claim. The host must refuse before starting the
container, preserve the claim, and reject replay after the policy is restored.

## Remaining normative gates and acceptance limits

- REQ-001/002: protected root intent, native initial stores, one-use Docker
  and child claims and exact source verification have positive isolated proof.
  Uncertain starts deliberately remain poisoned. A general authorized
  reconciliation workflow for partially launched existing instances is not
  implemented, and a synthetic new container cannot establish that some
  unrelated pre-existing installation has no hidden historical startup path.
- REQ-003: real Docker CREATED/never-started facts, private network/PID
  namespaces, no Docker auto-restart, the host guard challenge and new owner
  flock are verified in the isolated profile. This does not exclude arbitrary
  external operators or host-level services with independent Docker authority.
  Existing prior-installed manager cutovers require the separate known-prior
  fence/profile; no prior lifetime is invented here.
- REQ-004: the isolated NEW child and real retained *synthetic* OAuth grant
  pass the existing authenticated exact-child reader. Real production OAuth
  credentials are intentionally not mounted into tests. Negative mutation,
  revocation and stale-readiness regressions must remain effective.
- REQ-005: the real isolated host checks admission contention and crash/claim
  boundaries, an actual separate-UID refusal, live in-scope Docker network
  listener/process rejection, and a process entering during the held claim.
  The host's Engine HTTP 204/304 boundary also rejects an out-of-band Docker
  start during the final check-to-effect race instead of attributing it to
  this original operator. A live Docker restart-policy change during the held
  admission is likewise refused without child launch.
  Broader independent proof of hostile/external host startup sources, unknown
  prior installations and all interrupted/uncertain reconciliation branches
  remains necessary before treating the full issue as complete. Exact-head CI
  and independent review are still merge gates.

The #2668 missing-history recovery contract is unchanged: these commands
must never convert a missing old history into a NEW origin. No production
worker ownership, beta/release deployment, grant recovery or rollback is
authorized. Preserve all old bytes, records and unresolved claims rather than
erasing them to make an assessment pass.

## Verification and observability

Run `bash scripts/test.sh --ci-tests-only
tests/test_management_new_instance_preflight.py
tests/test_management_new_instance_begin.py
tests/test_management_new_instance_profile.py
tests/test_management_new_instance_admission_fence.py` for real subprocess/filesystem
coverage. Audit-hook barriers pause actual configuration opens to demonstrate
simultaneously in-flight scans and crash/replacement behavior; they do not
fabricate owner contention, grant authority or authenticated application success.
The additional guard-lock tests establish overlapping attempts at the same
Linux `flock` inode and show that normal or SIGKILL-released locks cannot
confer first-launch eligibility. They do not establish first-launch-claim
contention or authenticated application readiness.
The `tests/test_management_new_instance_docker_created.py` and
`tests/test_management_new_instance_first_launch_docker.py` suites are marked
`supervisor_integration` and exercise the real host Docker engine, including
positive physical child launch, synthetic OAuth readback, concurrent first
claim, race-window in-scope prior-process rejection, separate effective UID,
and replay/crash refusal. The latter requires a valid
`AUTOCODER_FRESH_TEST_BASE_IMAGE` integration image (defaulting locally to
`auto-coder:management-local-abc65544`), a non-root
Docker-enabled test host, and isolated disposable filesystem roots. Never
point these tests at a production OAuth root or existing container.

These operations do not expose a new public endpoint, create a worker job,
enable a deployment or assert dashboard success. Read-only assessment stdout
is never translated into worker readiness. The existing production trace and
dashboard contracts remain unchanged; see `docs/dashboard-observability.md`.

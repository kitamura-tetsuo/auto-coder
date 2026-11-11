# NEW-instance assessment and durable origin (DRAFT)

Issue #2672's Objective remains: “Allow an operator to initialize a new management
bootstrap while retaining existing OAuth state.” This checkpoint implements only
an independent read-only assessment and a separate, explicit one-use protected
origin record. It does not complete REQ-001–005 or authorize a first launch,
recovery, deployment or rollback.

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
or log file. A distinct admitted NEW first-launch controller is still required;
this selector alone never enables a process start.

The bounded profile requires the selected runtime's `source_root` to name the
same immutable application closure supplied in the origin request. Installed
runtime profiles, archived exclusions and container-only runtime bindings are
not yet supported by this check. The result is `profile-verified-only` with
`launch_authorized=false`: neither configuration parsing nor a valid retained
OAuth database proves an active grant, current owner, host-guard challenge,
first-launch claim, authenticated readback or any readiness. A separate actual
admission and first-launch consumer is still required under REQ-001–005.

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

This primitive is only local serialization for a future admission consumer.
It is **not** the supervisor's `.supervisor-owner.lock`, and does not exclude
any earlier manager or startup path unless that path is actually constrained
by independently verified host authority. Issue #2672's no-prior-lock profile
requires affirmative trusted host proof that no prior configured owner,
child or listener exists **and** continuous enforceable exclusion of prior
startup through first launch. A missing lock, scan or operator assertion
alone does not prove this. The authoritative host producer is not yet
integrated, so the probe cannot authorize launch even when it obtains the lock.

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
management child launch. The separate host controller, durable admission
claim, peer-authenticated challenge and actual exact-child readiness are still
required. Tests use disposable synthetic OAuth and containers only; they do
not attach real production OAuth or affect beta/release.

Independent native Linux subprocess tests hold two writers immediately before
the real O_EXCL call to establish one winner, and SIGKILL a producer immediately
after the actual open to establish a non-reusable partial origin. These are
positive tests of the bounded durable-origin producer, **not** the required
positive first-launch/owner/OAuth acceptance scenarios.

## Missing normative gates and acceptance evidence

- REQ-001: a protected origin record now exists at the host boundary, but
  provisioning actual new state/operation/guard stores and admission-bound
  root verification for first launch remain missing. An
  assessment neither proves unused instance identity nor OAuth grant usability.
  The optional profile check verifies retained schema/configuration selection,
  not a currently authorized read grant.
- REQ-002: a one-use original intent record now exists, but actual installed
  runtime/configuration agreement, immediately-before-launch identity checks,
  atomic first-launch claims, crash fencing and evidence-preserving launch
  reconciliation remain missing. The JSON output is not a durable initialization intent, consumable
  claim, or evidence of independently verified no-launch state. Never feed it
  into the existing cutover claim path as prior-history evidence.
  The optional bounded profile check is an independent read-only selection
  comparison, not a held admission-time identity guarantee.
- REQ-003: authoritative prior process/listener absence, relevant namespace
  evidence, known-prior or genuinely-NEW host profile and continuous exclusion
  of old startup sources, live supervisor owner fence, and fresh protected
  host-guard peer/challenge binding are missing. The transient guard lock and
  Docker CREATED-state snapshot do not establish continuous prior exclusion
  or constitute admission authority.
- REQ-004: new child/operation/handoff/recovery stores, first launch, exact-child
  observation, Bearer challenge and authorized fresh readback with current grant
  revalidation are missing. Existing #2668 recovery remains unchanged and refuses
  missing history; this command cannot bootstrap or reset it.
- REQ-005: full isolated production-like initialization with retained usable OAuth,
  separate-UID admission, live prior process/listener and evidence failures,
  actual overlapping owner/admission/claim contention, crash reconstruction,
  independent OS application-launch counts and authenticated readback remain
  unverified. The process regressions establish read-only assessment plus a one-use
  origin file with real origin-write contention and interrupted publication,
  not a validated actual launch or retained OAuth grant.

The first three Issue acceptance scenarios remain unresolved: there is no
admitted initialization, first launch or authenticated end-to-end proof here.
The fourth is preserved by leaving recovery unchanged and running its bounded
missing-history regression. No production effects, authorization, grant recovery,
worker ownership, host-controller operation, beta/release deployment, or rollback
procedure is added. Preserve all old bytes and records and all existing unresolved
claims; never erase them to obtain a fresh assessment. Deployment and rollback
still require separately authorized operator procedures and the unchanged full
normative gates.

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
The separate `tests/test_management_new_instance_docker_created.py` suite is
marked `supervisor_integration` and exercises the real host Docker engine
(`docker create`, guarded inspection and disposable removal), including a real
first Docker start that must invalidate CREATED-state evidence. It does not
prove a management supervisor's first child launch or grant readback.

This host-only diagnostic is observability-neutral for production: it creates a protected local origin record but no job, deployment admission,
launch, outcome, authorized resumption path or structured dashboard event. Its stdout assessment is never translated into dashboard readiness or
worker success. The existing production trace emissions and dashboard contract
remain unchanged; see `docs/dashboard-observability.md`.

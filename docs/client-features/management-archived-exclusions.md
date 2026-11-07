# Management cutover with archived owner and key exclusions

A second fresh cutover must preserve the fences and duplicate suppression of
all ancestors even when their old online history is no longer needed. This
extension retains the existing direct-prior journal behavior and treats older
journals only as exclusions. Their original databases, initialization records
and complete histories remain unchanged on disk. They are never merged into
new databases, dispatched, or projected through old-operation queries/replays.

The optional `archived_exclusions` bootstrap JSON array contains exactly these
fields per ancestor, ordered from the predecessor's direct prior to its oldest
inherited exclusion:

```json
{
  "instance_id": "manager-a",
  "host_journal_root": "/private/manager-a/journal",
  "journal_root": "/management-excluded/0/journal",
  "database_device": 1,
  "database_inode": 2,
  "host_fence": "/private/manager-a/state/.supervisor-owner.lock",
  "fence": "/management-excluded/0/owner.lock",
  "fence_device": 1,
  "fence_inode": 3
}
```

Values above are illustrative, never deployment evidence. Container mount names
use the actual zero-based list index. The list is bounded to 32 complete entries;
overflow, omissions, duplicates, conflicting identities and overlaps refuse,
never truncate. Successor bootstrap bytes are already pinned by the original
fresh initialization contract. No fields are added to, or rewritten in, an old
initialization intent. Legacy no-archive configurations still read unchanged.

The host derives the complete list from the captured predecessor's unchanged
bootstrap digest, protected fresh intent, successful initialization outcome and
exact readonly supporting mounts. It checks the actual database file identity,
not just its parent directory. Already-fresh predecessor profiles are accepted
only with the exact inherited mounts justified by that provenance. There is no
permissive mount prefix or caller-supplied path override through MCP.

After an approved read-only `fresh_host capture`, the verified host interpreter
can describe the required non-secret list without creating a claim:

```sh
python -I -B -m auto_coder.management.exclusion_host \
  --evidence-file /private/guard/prior-evidence.json
```

Put the reviewed list into a **new**, immutable successor bootstrap before its
new initialization intent is prepared. Mount every listed journal and lock from
its exact host source read-only and unshadowed. Preserve the existing direct
prior mount, OAuth root, policy and worker bridges. Do not edit the predecessor's
bootstrap, intent, journal, OAuth or watcher records to make validation pass.

Every archive must be fully readable, with a valid schema, complete history and
terminal operations. Unknown/active/NEEDS_ATTENTION work cannot be hidden in an
archive. An archived worker key returns conflict without old payload or effect;
management keys are checked against each archive's original management instance,
not the successor's namespace. Unrelated keys can proceed. Unavailable or changed
archive evidence closes admission and effect dispatch, including current intents
admitted before a later evidence loss. Public queries retain direct-prior
behavior; older archive IDs return no old history. A record being terminal does
not remove its duplicate-suppression obligation.

Both guarded-store admission and dispatch of already accepted management rows
use each retained generation's original instance namespace. A matching old key
therefore blocks candidate compatibility execution and durable handoff creation,
not only the later physical-stop consumer. Actor and OAuth client separation
remain part of the key namespace; unrelated keys can still proceed. Worker
targets continue to use their original worker namespace without retargeting.

## Continuous host session and successor proof

Deploying this code is a separate approved immutable bootstrap deployment. It
cannot be hot-patched into an older application closure. Review exact image,
configuration, database/lock identities, endpoint and recovery conditions first.
The host command below observes Docker only; it never stops or starts a service.

Keep the predecessor's exact network namespace FD open from capture. Create the
separately named successor container and its new private state/guard roots under
the existing fresh-initialization procedure, retaining all old container objects.
Before manually stopping the predecessor, run one foreground session:

```sh
python -I -B -m auto_coder.management.exclusion_cutover \
  --intent-file /private/new-intent.json \
  --evidence-file /private/guard/prior-evidence.json \
  --bootstrap-config /private/successor-bootstrap/config.json \
  --prior-lock /private/predecessor-state/.supervisor-owner.lock \
  --prior-network-fd "$OLD_NETWORK_FD" --guard-dir /private/guard
```

It acquires each ancestor's shared flock while the predecessor still holds its
own independent shared ancestor locks. Before announcing readiness it verifies
the same captured running lifetime on both sides of actual owner WRITE and all
ancestor READ lock observations; an already stopped or changed predecessor
refuses before claim. It then prints
`ancestors-fenced-awaiting-manual-stop`, and waits at most 300 seconds by default
(`--wait-seconds`, positive and at most 3600). The separately approved operator
stops only the old proxy and management container, preserving workers. The
session then acquires the direct predecessor's shared fence, verifies exact
process/cgroup/network/listener absence, claims the new empty state once, and
serves its existing one-shot host grant without releasing any ancestor lock.

The successor holds its own exclusive owner lock and independently opens shared
locks for **all** older owners before requesting the grant. Separate opens use
compatible shared locks; no same-open-file-description inheritance, lock downgrade
or SCM_RIGHTS transfer is used. The host verifies every descriptor identity,
readonly mount and the successor's actual kernel `FLOCK READ` entries joined to
its exact process lifetime before grant. Only then may the child be admitted.
The host and successor overlap their shared holds, excluding every old EX owner.
Reconstruction reacquires the same sealed set. Partial acquisitions release
acquired descriptors before a child effect; an uncertain launched child keeps
its fences until process-lifetime resolution.

The old individual `fresh_host claim`/`guard` routes refuse inherited exclusions:
two independent CLI lifetimes cannot prove continuous ownership across their gap.
If this host session is interrupted after a claim, preserve the claim and all
observations. A new command must not erase, reuse or repair that partial root.
Loss of all holders is not claimed to preserve a kernel lock; a missing grant or
unresolved claim prevents the successor's authorized launch. Re-observe exact
processes before any recovery; do not blindly replay a cutover.

## Verification and rollback limits

Verify protected HTTP 401, fresh exact-child authenticated readback, unchanged
OAuth identity/client/grants, direct-prior behavior, archived-key refusal, watcher
attachment and unchanged worker supervisor/run/deployment identities. A namespace
match, a stage receipt or a terminal record count alone is not readiness.

Old databases and images remain available. Before the successor accepts any new
operations, rollback requires actual successor exit, preserved all-generation
fences, proven absence, compatible retained OAuth and explicit handling of any
launch claim. Never restore database snapshots or delete journals. After new
operations are admitted, simply restarting an old manager would lose awareness
of the successor's journal; use a reviewed forward cutover that also excludes
that new generation, or a separately proven compatible recovery. This extension
does not authorize an automatic rollback, worker restart or beta deployment.

Tests exercise actual SQLite, authenticated MCP, independent kernel lock holders,
real supervisor regressions and controlled host-observation boundaries. A
multi-generation live Docker cutover, host reboot, deployment rollback and power
loss require separate operator-controlled verification; unit controls do not
certify those operations. Existing host status/refusal output and typed
CONFLICT/UNAVAILABLE responses are retained. No worker scheduler/provider or
dashboard event schema changes are introduced.


The dedicated `test_management_archived_cutover_container.py` cases run in the
existing root `fresh-host` CI group with only disposable UUID-named containers,
OAuth grants, database roots and image tags. They exercise the unpatched host
session across A/B/C, old management-key refusal and non-projection, direct-prior
replay, clean C reconstruction, preserved journal/intent bytes, and failures for
missing ancestry, stopped predecessors and an interrupted host grant. Old-owner
competitors use the real entrypoint and original state/config without published
ports, so an actual `BlockingIOError`, rather than a port bind collision, is the
refusal oracle. These cases do not access the optional deployed-proxy fixture or
perform a production cutover. Their implementation/collection is not evidence of
a successful container run; require their exact-head CI results before activation.


The immutable supervisor reopens provenance-validated retained evidence in its
own authority check before each forward handoff effect, both SUBMITTED-to-stop
and STOPPED-to-candidate-start. It checks management keys using each predecessor's
original manager namespace. Losing the evidence after child dispatch cannot
permit a queued forward stop/start. If the original child was already stopped,
the existing single exact-original-runtime safety return remains available;
this is not permission to resend the candidate or release an unresolved lane.

Successful archived-instance reconstruction verifies all retained journal
contents and reacquires every ancestor and direct-prior fence even when
`restart_after_clean_shutdown` is omitted or false. That flag still controls
only automatic clean child restart. Invalid or incomplete evidence and partial
lock acquisition fail before supervisor publication; acquired descriptors are
released on failed reconstruction. The existing uncertain-first-launch fence
retention behavior is unchanged. Component regressions use real original grants,
policy and A/B/C SQLite plus explicit physical-adapter/mount controls; they do
not substitute for the dedicated container CI cases.


The archived-container fixture now uses only newly generated Caddyfiles with the
pinned CI Caddy binary in a disposable bind-only fixture image; its SHA-256
is verified before and after copying from the pinned image. No mount gate is
relaxed for Caddy's optional default data volumes. Readiness is checked through the published proxy using
both HTTP 401 and authenticated exact-child observations. Its positive case
keeps the old Caddy namespace across C reconstruction, then starts the real
watcher with a new private journal and verifies proxy lifetime/namespace change
and restored authenticated HTTP. It never enables the deployed-proxy option.
The same case reuses the disposable two-channel worker network. Signed actual
observations pin both worker lifetimes. An opt-in fixture-only receiver counter
records no headers or bodies; a harmless signed POST positive control establishes
that it is live, and old worker keys must add zero POSTs before/after cutover and
C reconstruction. These are CI execution requirements, not local execution claims.

Fresh source-script profiles retain their previous eligibility restriction: a
source console wrapper is not an installed READY. There is no promotion command,
receipt manufacture, or rewriting of an existing initialization intent.

## Installed initial runtime binding (version 2)

A newly provisioned fresh instance may instead select a real
`management-installed` initial runtime. Its bootstrap configuration includes:

```json
"fresh_runtime_binding": {
  "version": 2,
  "bootstrap_application_root": "/opt/auto-coder/.venv/lib/python3.12/site-packages/auto_coder",
  "initial_ready_path": "/management-versions/revisions/…/…/MANAGEMENT_READY.json",
  "initial_ready_identity": "<64 lowercase hexadecimal characters>"
}
```

This is a new configuration sealed *before* the existing one-shot host claim.
The existing intent schema and bytes remain unchanged. Its bootstrap-config
hash seals this versioned binding. Its application digest authenticates the
original bootstrap package at the canonical read-only anchor; its executable
digest authenticates the initial installed Python. The anchor must be disjoint
from the version root and the active OAuth, journal and bootstrap-state roots.
Unknown versions, additional fields and incomplete binding profiles fail closed.
Missing/null binding retains the existing source-script behavior.

Prepare the initial installed environment with the existing
`ManagementVersionStager.prepare()` using the exact final management YAML,
instance, configured Python/ABI and source authority. Prepare the candidate with
the same procedure before activating it. Neither READY nor the initialization
intent may be edited to claim a different source, instance or configuration.
The fresh container may retain the existing read-only version mount: forward
selects an already prepared candidate through `lookup()` and does not invoke an
installer. Online preparation inside that read-only mount remains unavailable;
this contract does not add a writable supporting mount or weaken host checks. A
read-only container must also configure a private writable `TMPDIR` for the
existing compatibility preflight, for example the bootstrap-created
`/management-child-state/logs` inside its owner-only state root. This is part of
the immutable initial runtime environment; do not add files/directories to an
unclaimed empty state root to bypass the initialization checks.

Before consuming the host grant, immediately before first launch, during fresh
readiness observation, and during reconstruction, the owner reopens the original
READY through the configured stager. This verifies the authoritative original
source-test run, tag/lock, installed content, configured Python/ABI, instance and
management configuration. The initial READY and immutable anchor must remain
available even after subsequent child updates. Do not garbage-collect them.
`LD_*` and Python code-hook variables are refused; only the documented isolated
Python flags with value `1` are accepted. Protected credential file paths and
fixed worker bridge configuration retain their existing contracts.

The serving child's retained reader also authenticates the *currently selected*
READY and live PID/start-token, generation, nonce, argv and environment. Its
actual imported package must be inside that installed environment, which may
legitimately differ from the original bootstrap package. A bounded startup wait
permits the parent's Popen-to-RUNNING ledger publication, never a fallback to
owner-only validation. Owner/effect readers explicitly request the distinct
owner purpose. Disposable compatibility preflight removes bootstrap/live-state
paths, as before; it cannot impersonate the selected child.

Reconstruction reacquires every archive and direct-prior fence for v2 even when
clean-child restart is disabled. False/omitted restart does not launch a child.
Partial fence acquisition is rolled back; a possibly launched child retains its
existing protection. P1 stop and forward-start checks still reopen all retained
evidence. Loss after drain may make even the one exact-original runtime return
unable to serve: that is not success and must not become an automatic retry.
After a new operation admission, returning to an old instance is not a rollback
procedure; retain the current state and recover forward or stop for inspection.

The isolated fresh-host CI requires six installed A/B/C cases: successful update
to different installed code plus reconstruction/Caddy reconnect; ancestor loss
at real durable SUBMITTED and STOPPED boundaries; corrupt candidate READY;
host interruption; unavailable original source evidence before first claim.
All installations use the actual stager and disposable HTTP source/Firebase
services. Their local source records describe the fixture checks actually run,
not a claim that the public full-source workflow ran. Container execution is
required evidence; collecting those tests or component tests alone is not a
live-cutover GO decision. Preserve existing OAuth, original databases/intents,
all fences, old-key suppression and worker lifetimes throughout verification.


A successful fresh predecessor retains its original Docker command including
`--fresh-initialization`. Capture accepts only the exact supported command forms
with that single final flag, and only after its original sealed successful fresh
intent, bootstrap hash, retained roots and supporting mounts are verified. A flag
alone, unresolved initialization, extra arguments or changed provenance do not
qualify; process/kernel/container ownership checks still apply.

The isolated multi-generation fixture pins an explicit host bridge gateway while
retaining the canonical internal worker network. Installed fixtures prove real
HTTP authority reachability from both installer and child networks before
staging. Readiness startup resets may retry until the deadline, never count as a
challenge or authenticated proof. The uploaded diagnostic artifact includes
bounded operational state and filtered errors, excluding request headers/bodies
and credential strings. These observations diagnose a failed boundary; they do
not relax readiness or replace actual passing mandatory container cases.


Management publication and worker transport use separate fixture links. Workers
remain only on the canonical internal network. Each management generation uses
the normal bridge for its exact loopback-published Caddy port and also joins the
private worker network before starting. An internal-only endpoint is not a
Docker gateway endpoint for programming published ports; host reachability of
an internal bridge address does not imply a published port was installed.
The fixture checks actual `NetworkSettings.Ports`, and retains the existing
kernel listener, exact target IP, namespace and owner validation.

The stopped-predecessor recheck repeats successful sealed fresh provenance
verification before accepting its immutable final initialization flag, just as
the running capture does. It does not assume the prior command changed merely
because the same supported flag is still present after shutdown.

Sanitized diagnostics record connection exception type/errno, actual port
publication, listener addresses and management/proxy namespace identities.
They also emit a bounded safe summary to normal test output. Only the new
sanitized artifact directory/file is handed back to the invoking uploader's
ownership, retaining modes 0700/0600. Protected state, OAuth, journals and secret
permissions are never changed. Raw headers/bodies and credential strings are
excluded before publication; root-only runtime files are never uploaded.

The fixture networking choice follows Docker's gateway/external-connectivity
contract, rather than assuming requested PortBindings imply active forwarding:
[bridge Join](https://github.com/moby/moby/blob/v28.5.2/libnetwork/drivers/bridge/bridge_linux.go)
and [gateway selection](https://github.com/moby/moby/blob/v28.5.2/libnetwork/default_gateway.go).
The exact older CI URLError errno was not retained; subsequent runs must prove
actual mapping, Caddy challenge and authenticated child readback independently.

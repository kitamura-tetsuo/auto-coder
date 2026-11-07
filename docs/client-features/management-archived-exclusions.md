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

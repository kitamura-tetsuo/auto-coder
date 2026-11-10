# Prospective original management attempt provenance

The optional `fresh-original-attempts-v1` profile records original fresh-intent
admission through `claim_fresh_initialization`, shared by the host fresh command,
continuous exclusion cutover and bootstrap upgrade. It also gates
`serve_one_fresh_grant`, before any host listener callback can start the candidate
supervisor. The existing bootstrap creates its child ledger before requesting the
host grant; it creates the operation, handoff and recovery stores afterward.
Capture never grants recovery authority, changes a controller, stops a process,
or deploys an image.

This is **prospective, explicitly selected original-admission coverage**. It
cannot establish a deployment-wide frontier across earlier or unenrolled calls.
The initial predecessor is an external boundary, not proof that its missing
history was empty. `earlier_history` is always `UNAVAILABLE: before prospective
enrollment`. In particular this profile cannot make the current incident's
missing failed-generation journal recoverable. Deployment-wide enrollment
continuity and complete positive host acceptance remain unverified; do not use
this implementation as a recovery completeness prerequisite.

## Original producer and lifetime

An operator must separately provision a new canonical mode-0700 history directory
owned by the executing Unix user. Explicit local enrollment produces a binding:

```sh
python -I -B -m auto_coder.management.original_attempts \
  --root /private/new-original-history --deployment EXACT_DEPLOYMENT_ID
```

Retain the returned binding independently. Enrollment creates an exclusive lock,
protected authority and empty frontier; it does not inspect, convert or backfill
old histories. Re-enrollment, a partial enrollment or a nonempty directory refuses.
The source identity is the SHA-256 closure of the executing package's Python
source paths and bytes, not an inferred Git revision or an operator-supplied
success verdict. An unavailable or different source refuses capture and reading.
Only the exact installed source/profile is supported.

A newly prepared `FreshInitializationIntent` explicitly selects the profile with
both `original_history` (the canonical host directory) and
`original_history_id` (the returned UUID). Existing intents and ordinary fresh or
upgrade attempts remain unenrolled and retain their previous admission rules.
Never modify an original intent to enroll it or retry an uncertain target. The
history must be separate from all management stores, and no live enrollment or
configuration migration is performed automatically.

Under a nonblocking exclusive history flock, admission captures the complete
intent, predecessor/target/container identities, expected store paths, mounted
root device/inode identities and original producer PID, Linux start token, boot
ID and nonce. Original events are immutable numbered files chained by digest.
The atomically replaced, fsynced frontier commits the exact count and last digest.
The independent reader validates the original anchor and grant-possible
producer lifetime fields (positive PID, canonical Linux start token,
canonical boot UUID and nonce) even when an event and its frontier
digest form an internally consistent chain. A malformed lifetime or
unexpected terminal-boundary payload is unavailable, not a recorded
grant or successful original attempt.
Individual original files are bounded to 8 MiB; larger evidence is unavailable.
Protected original regular files, including the history lock, authority,
frontier, events, intent and native stores, must have exactly one hard link.
An otherwise matching digest/inode with an additional writable filesystem
name refuses independent verification; link-count changes during a read
also refuse. This is an original-byte alias guard, not evidence of complete
historical coverage or a recovery authorization.
The bounded namespace admits at most 4096 events and rejects overflow, extra
files, gaps and incomplete writes instead of truncating or repairing them.
Concurrent admission reports `history admission contested` before writing an
intent. Previously anchored request IDs, target aliases and cycles refuse replay.
An original target whose own admission ended `never-created`, `unresolved`, or
only anchored without a durable claim is not a valid predecessor for a new
covered attempt. Both the original producer and independent evidence consumer
refuse such a lineage instead of promoting a failed branch into an ancestor.
This check is limited to prospective capture and does not assert that a merely
claimed predecessor was successfully launched or ready.

An original refusal after affirmative pristine-root observation can durably
record `never-created` only while the same exclusive history lock is held, the
exact directories remain empty and no intent was written. The boundary binds the
same source/history, original lifetime and expected stores. All subsequent host
grants for that attempt refuse. Interruption before that terminal commit,
non-pristine roots or a partial intent remains unresolved. No reader creates a
terminal fact from an absent claim, database, process or row.

A completed claim records the original intent's identity and digest. The host
adapter durably records `grant-possible` before entering its existing checks or
launch callback, and holds the history lock for the entire adapter invocation.
That conservative boundary does not mean a grant was delivered. A failed or
interrupted adapter cannot be retried. After native store creation and before
child launch, the real bootstrap writes `fresh-original-stores.json` once, binding
the intent digest, source, bootstrap producer lifetime, history/request, owner
fence and exact four database identities. The independent consumer also
validates the receipt's canonical PID, Linux start token, boot UUID and nonce;
malformed receipt lifetimes remain unavailable even with unchanged store inodes.
Receipt production checks the current
bootstrap PID actually holds the original owner WRITE flock. Missing receipts or expected databases remain
unavailable. No original store is initialized by an evidence reader.

For this explicitly enrolled original-history profile, the real Supervisor
opens its own owner-lock inode with creation mode 0600 even if the invoking
operator process has a permissive umask. An existing owner inode that is
symlinked, multiply linked, non-owned, or not already 0600 is refused without
permission repair. While creating the first four native protected SQLite
stores and their original receipt, the Supervisor temporarily constrains the
creation umask to 0077 and restores the previous value on every exit.
Unenrolled/legacy Supervisor opening and startup semantics are unchanged.
A claim or grant failure still preserves partial native stores rather than
clearing them for retry.

## Read-only evidence interface

`HistoryBinding`, `OriginalAttemptEvidence`, `observe`, `derive` and `verify`
provide the Python interface. A separate consumer holds `observe(binding)` and
passes its returned descriptor to `derive(binding, descriptor, owner_fences)` or
`verify(submitted, binding, descriptor, owner_fences)`. For attempts that reached
the native store boundary, `owner_fences` must contain exactly one currently
held shared descriptor matching each original owner fence. These are the
consumer's own read-only observation resources, not an asserted Boolean. The
reader rechecks their file identities and kernel locks before returning. Each
original is parsed from the same read whose bytes are hashed, never from a
second unsynchronized read attributed to an earlier digest. The verifier checks
this exact open file description's current kernel READ flock and reopens the
original authority, complete frontier, original intents, native store receipts,
databases and retained fresh records. It compares exact protected identities and SHA-256 digests and
rejects stale, truncated, forged, cross-deployment or mutated evidence. Malformed submitted JSON structures are uniformly refused at the CLI boundary without a Python traceback; they never authorize a result. A returned
object contains every covered attempted branch, explicit unavailable reasons,
expected stores and source/history bindings. The only supported nested path
is the Supervisor's same-UID, nonsymlink `state/logs` directory: its contents
are explicitly diagnostic, not original authoritative admission or grant
records. Any other nested directory or symlink in the original roots refuses.
Expected SQLite stores, intent/claim/receipt files and all other root-level
originals remain mandatory, fingerprinted and independently reopened; missing
or uncheckpointed native WAL evidence still refuses. It has no completeness
Boolean or launch/epoch authorization. Earlier history remains unavailable even when the
bounded submitted object matches.

The independently retained binding can also be supplied to a separate process:

```sh
python -I -B -m auto_coder.management.original_attempt_evidence \
  --binding /private/original-binding.json
python -I -B -m auto_coder.management.original_attempt_evidence \
  --binding /private/original-binding.json --submitted /private/earlier-evidence.json
```

The CLI also accepts repeated `--owner-fd` options for inherited, currently
held shared owner descriptors; it never obtains process authority on the
consumer's behalf.

Exit 1 means unavailable or refused, including legacy absence, unsupported source
or profile, corrupt/conflicting frontier, changed file/lock, unresolved outcome,
nonempty WAL/rollback journal or omitted expected store. The reader uses immutable read-only
SQLite connections, rejects unresolved WALs and rollback journals and does not create sidecars.
Observation resources close on refusal. Neither an earlier verification nor a
released observation claims current exclusion; consumers still own their actual
deployment fences and operation-history policy.

## Evidence and remaining limits

Run `bash scripts/test.sh --ci-tests-only tests/test_management_original_attempts.py
tests/test_management_original_predecessor_guard.py
tests/test_management_original_bootstrap_integration.py
tests/test_management_original_native_process.py
tests/test_management_fresh_launch_exclusions.py`
for bounded tests, then run the selected `supervisor_integration` Docker case
`test_management_fresh_end_to_end.py::test_fresh_first_launch_via_host_guard_and_authenticated_http[False-False-None-True-False-False-True]`
on an authorized root-enabled disposable Linux/Docker host.
for real isolated production-origin claims/refusals, original-byte fingerprints,
separate-process verification, and observable process/flock contention. The
isolated native bootstrap integration invokes the actual Supervisor
`_start_fresh_initialization` producer and its four native SQLite
initializers on Linux, then independently reopens their original 0600 files
and owner inode. The separate store consumer checks actual database schemas,
original source/history/intent and retained OAuth bytes, and an incompatible
prior owner refuses. Its host grant transport and physical child response
are deliberately substituted: the resulting `claimed` frontier still reports
`original grant boundary not crossed` and is **not** AS-L01, successful
first-launch, external container evidence or a verified recovery epoch.

The additional `test_management_original_native_process.py` fixture drives the
actual Supervisor **CLI**, exclusive owner-fence opening, four native SQLite
stores, original receipt, durable first-launch claim, and a real disposable
Linux child process through the genuine `Popen` owner. The physical child
exits early and is independently counted as a single launch. Its original
`original-history` profile is installed by the fixture's actual
`claim_fresh_initialization` writer. A distinct verification process checks
the retained attempt while reporting `original grant boundary not crossed`.
The fixture still substitutes the host-guard challenge transport and the
installed management executable's physical argv with a disposable gate:
**this does not establish original host-grant, closed deployment/controller
authority, actual application readiness, AS-L01 success or Docker acceptance**.

### Isolated real Docker original admission and consumer

A separately marked **real Docker/host-root** case in
`tests/test_management_fresh_end_to_end.py` exercises the supported original
history profile through the actual `fresh_host claim` and foreground
`fresh_host guard` entrypoints, the real isolated replacement Supervisor,
first-launch claim, four protected native stores, application child, protected
HTTP endpoint, retained OAuth token and authenticated exact-child readiness.
The host-side original frontier records real `anchor`, `claimed` and
`grant-possible` events. The application reports a genuine
`fresh-original-stores.json` tied to that attempted source and owner lifetime.
A second real original `claim_fresh_initialization` writer then refuses a
distinct successor with a deliberately mismatched retained OAuth root and
durably records `never-created` without an intent, grant or launch. The
resulting reverse frontier includes both the successful launch's original
admission and the separate failed successor.

An additional same-source disposable case establishes actual original-profile
first-launch-owner contention at a held durable claim barrier: an independent
Supervisor competes while the first owner holds its exact lock, is refused
without an additional child transition, and the original owner later completes
authenticated readiness and independently verifiable lineage. Another
disposable case interrupts the original-profile owner **after** its actual
durable first-launch claim, kills/restarts the exact Docker instance, and
checks that reconstruction cannot relaunch the child and the independent
original reader still reports incomplete evidence. These are real Docker/OS
observations, not hand-authored successful grant records.

A **cleanly stopped** disposable Supervisor releases its owner lock and
checkpoints the SQLite WALs. A separate process inherits the currently held
shared owner lock, independently reopens the original root, exact native
databases and protected file digests, and verifies the complete covered
frontier. A forced Docker stop that leaves a nonempty WAL **remains
unavailable**, not retrospectively repaired, and a diagnostic log directory
is never treated as original grant authority. The fixture's local identity
provider controls only its disposable test OAuth login; real OAuth token
issuance/usage, Docker process/lifetime, host guard and API responses are
observable independently.

This is **positive isolated production-origin capture and verification for
one covered successful first launch plus a genuinely refused successor**,
not a proof of enrollment of all deployment sibling origins, a complete
pre-enrollment/failed-generation history, a second real successful A-to-B-to-C
cutover, or authorization for #2824, #2825 or the present incident.
No production/beta management, OAuth, worker or Docker controller state is
modified by this disposable fixture.

Production
Loguru boundary emissions supply barriers after actual fsync, allowing an owner
to be interrupted at anchor, claim, terminal and possible-grant boundaries while
a contender demonstrably refuses. Hand-written malformed files are negative
fault controls only. They are never positive original-history evidence.

These tests do not demonstrate a successful Docker-host A→B→C cutover, complete
retained bootstrap/ancestor record derivation or deployment-wide enforcement
against an unenrolled sibling origin. The native host socket/namespace and
upgrade tests can fail in restricted execution environments; unavailable host
behavior is not a successful acceptance result. No real management host, OAuth,
workers or controller configuration is changed by this test selection.

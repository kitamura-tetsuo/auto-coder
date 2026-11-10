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
Individual original files are bounded to 8 MiB; larger evidence is unavailable.
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
fence and exact four database identities. Receipt production checks the current
bootstrap PID actually holds the original owner WRITE flock. Missing receipts or expected databases remain
unavailable. No original store is initialized by an evidence reader.

## Read-only evidence interface

`HistoryBinding`, `OriginalAttemptEvidence`, `observe`, `derive` and `verify`
provide the Python interface. A separate consumer holds `observe(binding)` and
passes its returned descriptor to `derive(binding, descriptor, owner_fences)` or
`verify(submitted, binding, descriptor, owner_fences)`. For attempts that reached
the native store boundary, `owner_fences` must contain exactly one currently
held shared descriptor matching each original owner fence. These are the
consumer's own read-only observation resources, not an asserted Boolean. The
reader rechecks their file identities and kernel locks before returning. The verifier checks this exact open
file description's current kernel READ flock and reopens the original authority,
complete frontier, original intents, native store receipts, databases and retained
fresh records. It compares exact protected identities and SHA-256 digests and
rejects stale, truncated, forged, cross-deployment or mutated evidence. A returned
object contains every covered attempted branch, explicit unavailable reasons,
expected stores and source/history bindings. It has no completeness Boolean or
launch/epoch authorization. Earlier history remains unavailable even when the
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
nonempty WAL or omitted expected store. The reader uses immutable read-only
SQLite connections, rejects unresolved WALs and does not create sidecars.
Observation resources close on refusal. Neither an earlier verification nor a
released observation claims current exclusion; consumers still own their actual
deployment fences and operation-history policy.

## Evidence and remaining limits

Run `bash scripts/test.sh --ci-tests-only tests/test_management_original_attempts.py`
for real isolated production-origin claims/refusals, original-byte fingerprints,
separate-process verification, and observable process/flock contention. Production
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

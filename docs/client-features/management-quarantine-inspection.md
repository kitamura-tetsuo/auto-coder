# Inspection-only quarantine of original management operation histories

This command is a **read-only historical inspection**, not a repair, fresh
initialization, ownership enrollment, successful handoff, merge/deployment
permission, or process controller. It never creates an operation or admits an
old row to a new dispatcher. The supplied manifest is only the list of sources
to inspect; it cannot prove that all former, failed or alternate management
attempts were included. Missing evidence means **unavailable**, never empty.

Run it as the owner of the original state, with original files preserved,
after preparing a private (`0600`) JSON manifest binding each database and
owner lock to its observed device/inode/ctime and database SHA-256 digest:

```json
{
  "version": 1,
  "journals": [{
    "instance_id": "original-manager",
    "database": "/protected/original/management-operations.sqlite3",
    "fence": "/protected/original/owner.lock",
    "database_device": 1,
    "database_inode": 2,
    "database_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "database_ctime_ns": 111,
    "fence_device": 1,
    "fence_inode": 3,
    "fence_ctime_ns": 222
  }]
}
```

The values above are **illustrative**, not evidence from any deployment.
Use no symlinks or group/world-readable files; do not use a manifest from an
untrusted requester. Run from an installed matching source revision:

```sh
python -I -B -m auto_coder.management.quarantine_history --manifest /protected/manifest.json
```

The command returns an inspection-only report with
`launch_authority=false` and `lineage_completeness=unverified`. It exits
nonzero with `status=unavailable` for missing, inconsistent or changing
protected evidence. For in-process API use,
`QuarantineHistory(load_manifest(path))` holds separate shared owner-flock
descriptors during its lifetime, revalidates the **entire** supplied journal
set on every `inspect` or `decide` call. After all database and sidecar
reads, it also rechecks each original held descriptor and its named owner fence
against the manifest, including earlier fences after later journals. If a fence
is replaced mid-inspection, the call refuses even if the databases are unchanged;
a new pathname's lock cannot substitute for the original hold. Descriptors are
released on close, including after partial-acquisition failure.
The three bounded decisions are `key_excluded`, `target_blocked`, and
`no_conflict_in_supplied_journals`. No old row, replay permission, complete
lineage or executable operation is exposed by these decisions.

Databases are copied through checked read-only file descriptors into an
in-memory SQLite validation connection. Original SQLite databases and their
WAL, rollback-journal and SHM sidecars are never opened by SQLite; a nonempty,
unreadable or changing sidecar refuses. This is a fail-closed inspection of
supplied original bytes, not continuity of historical ownership after every
flock holder exits. It does not inspect or control Docker, the OAuth database,
proxy, controllers, active child processes or their independent clocks.

See also `management-archived-exclusions.md` for the *separate* existing
fresh-cutover archive contract and the recovery-epoch issues for independent
lineage, containment, grant and authenticated launch requirements. These
pre-existing admission paths are unchanged by this feature.

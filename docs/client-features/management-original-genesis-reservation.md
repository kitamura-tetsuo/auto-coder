# Prospective ordinary genesis root reservation (incomplete prerequisite)

Issue #2839 remains incomplete and must remain draft. This component is a
non-authorizing storage prerequisite for original genesis capture, and for
#2824; it does not establish original genesis or make either issue ready.
The existing `fresh-original-attempts-v1` profile and its external predecessor
boundary are unchanged. No existing receipt, empty root, stopped container or
missing claim becomes evidence that an earlier launch never happened.

## Supported interface and facts

`auto_coder.management.original_genesis_reservation` exposes the Python-only
`reserve(path, deployment)`, `observe(binding)` and `verify(binding, fence)`
interfaces. There is deliberately no deployment command or bootstrap consumer.
The supported component profile is Linux `/proc` with local filesystem flock,
exclusive mkdir/file creation and file/directory fsync, running the exact Python
package source closure returned by `original_attempts.source_identity()`.
This is not yet a supported ordinary local-system-Docker genesis profile.

`reserve` requires an explicit deployment scope and an absolute canonical,
previously nonexistent path directly beneath a same-UID mode-0700 parent.
It creates its own mode-0700 namespace and separate `state`, `journal` and
`history` directories. It never accepts precreated empty roots as new originals.
The reservation exclusively creates a mode-0600 `reservation.lock` and holds
its kernel WRITE flock during creation and final observation. It publishes
one canonical mode-0600 `reservation.json` with exclusive creation, file fsync
and directory fsync, binding the producer PID/start token/boot ID/nonce, source,
deployment scope, exact path, root device/inodes and lock identity. Namespace
creation is also fsynced in its parent, and each new root is fsynced before
record publication. The returned frozen `ReservationBinding` additionally pins
the record inode and SHA-256. Retain that binding independently: reading a
record from disk and inventing a matching binding is not verification of origin.
The producer does not claim the recorded lifetime is still alive on a later read.

`observe(binding)` opens a temporary shared reservation flock. `verify` requires
that exact descriptor's current kernel READ flock, checks protected identities,
reopens the original record bytes, checks its canonical shape and source closure,
and rechecks the bytes, root namespaces and fence before returning immutable
`ReservationEvidence.original` bytes. The lock must retain its original empty
bytes. Symlinks, hard-linked regular files, changed records/roots/locks, added
entries, permission changes, foreign bindings and partial capture refuse.
Readers create no files or SQLite sidecars and never repair or remove evidence.
This reservation fence is neither a history fence nor a Supervisor owner fence.
It proves no continuous exclusion after release and supplies no launch authority.

Every returned evidence object explicitly carries three unavailable producers:
exact engine/created-never-started manager/proxy observation; ordinary bootstrap
genesis/native stores/first-launch originals; and a genesis-bound successor
frontier. There is no completeness, readiness, epoch, recovery, grant or
never-created field. An empty reserved directory describes only this observation;
it says nothing about prior deployment attempts or an unknown first outcome.

Any interruption after namespace creation poisons that path for further capture,
even if a fully readable record reached disk before the caller received its
binding. No replay, resume, relocation, cleanup or reconstruction API exists.
An independently retained binding whose expected original is missing refuses;
absence is not a terminal no-launch outcome. Reservation roots must not yet be
used by bootstrap or enrolled using the older history CLI: that would invalidate
the empty-root reservation observation and would not close the missing genesis
contract. No production bootstrap, fresh, upgrade, cutover, NEW/private-engine,
OAuth, worker or recovery integration is changed by this component.

## Issue contract and remaining work

| Contract | Evidence supplied here | Still unimplemented for #2839 |
| --- | --- | --- |
| REQ-501 | Explicit scope; exclusively producer-created protected roots | Independent exact engine, full created/never-started manager/proxy, immutable image/source/command/config/mount observations; actual selected ordinary profile |
| REQ-502 | One-time canonical fsynced reservation, exact root/file identities, original producer lifetime, no repair/reuse | Complete original genesis and history authority joined to authoritative Docker observations; independent genesis ownership across bootstrap |
| REQ-503 | None; reservation cannot authorize bootstrap | Actual ordinary bootstrap genesis verification before effects, exclusive owner lock, four native stores and original first-launch boundary; unknown-outcome retry refusal |
| REQ-504 | None; older prospective external-baseline behavior unchanged | Genesis binding at every supported fresh/upgrade/cutover admission, all failed branches and advanced/omitted/foreign binding refusals |
| REQ-505 | Separate read-only reservation observation under a real current reservation fence, immutable bytes and mismatch refusals | Genesis plus full retained frontier under history and native owner fences; exact container/store identities; verifiable complete unknown outcome distinguished from missing expected stores; retained #2823 exclusions |
| REQ-506 | Isolated component process/filesystem probes only | Genuine selected ordinary bootstrap and Docker successor/failed-branch acceptance, each origin's omission tests and independent preservation/no-launch/no-grant observations |
| REQ-507 | Component interface, explicit gaps and diagnostic trace documented and tested | Full source/profile and production trace/dashboard coverage as the missing producers are implemented |

AS-G01 through AS-G05 are **not satisfied**. In particular, reservation tests
cannot substitute for the actual positive ordinary bootstrap in AS-G01, selected
Docker observation in AS-G02, complete-but-unknown native evidence in AS-G03,
successor-origin coverage in AS-G04, or original bootstrap/receipt producer kills
in AS-G05. A future integration must supply those producers and tests; neither
this document nor tests redefine those requirements.

## Bounded validation and observability

Run `bash scripts/test.sh --ci-tests-only
tests/test_management_original_genesis_reservation.py` for the real filesystem
producer, separate-process reader, mutation/alias/foreign/missing-original
refusals and current-lock checks. The interruption probes execute the actual
producer in a subprocess and wrap `os.fsync` to stop **after the real syscall**
at each of its eleven publication boundaries. They establish a live producer
before attempting a competing reservation, kill it, and assert unchanged partial
bytes and refusal to reuse. A separate OS lock probe independently demonstrates
material WRITE/READ contention while the producer is held before its final read.
The deterministic namespace-interleaving test is a unit race probe. These are
component tests, never Docker or native bootstrap acceptance or crash/power-loss
durability certification.

After all durable writes and final checks, the shared Loguru configuration emits
`genesis_reservation`, `genesis_boundary=roots-reserved`, `evidence_only=true`
with a message explicitly stating that original genesis remains unavailable.
The existing colored console and rotating file sinks retain filename/function/
line metadata. Read-only verification emits no success or recovery event.
The runnable trace assertion is in this component test file; the corresponding
production-to-view boundary is recorded in `docs/dashboard-observability.md`.

# Jules Competition-Generation Ledger

The Jules competition-generation ledger represents competing Jules
executions dispatched for the same source Issue attempt as one durable
logical **speculative generation**, with durable candidate-identity records,
a single-winner selection boundary, and durable retirement of every
non-winning result (GitHub Issue #2070, a stage of the Jules multi-candidate
competition family #2069).

This module owns only the provider-independent durable state model and its
transitions. It does not itself start Jules sessions, poll provider state,
choose CI policy, evaluate CI, merge pull requests, or read/write
`cloud.csv` or any other legacy single-session binding — those integrations
belong to later stages. The supported deployment is one authoritative
controller with concurrent workers and restarts; consensus between
independent controllers is out of scope.

## Namespace and Scope

State is keyed by repository slug and source Issue number. A speculative
generation additionally carries the source attempt number, a fixed positive
candidate count with distinct candidate identities, the dispatched
Issue-oracle snapshot and fingerprint, the requested source branch, and
captured policy settings. At most one **active** generation may exist per
repository/Issue at a time; a second admission attempt is denied without
resizing or replacing the existing candidate set. Replaying the same
operation ID and bundle ("resuming" a generation) is idempotent and never
creates or resizes the candidate set.

## Candidate Authority States

Each candidate within a generation has a durable authority state, distinct
from any provider-observed artifact:

- `NEVER_SUBMITTED` — initial state.
- `SUBMISSION_CLAIMED` — a worker has claimed submission of this candidate.
- `ACCEPTED` — the provider accepted the candidate, with a recorded
  provider/session identity.
- `DEFINITELY_NOT_ACCEPTED` — the provider definitely refused the candidate.
- `SUBMISSION_OUTCOME_UNKNOWN` — the submission outcome could not be
  observed. This state can never overwrite a more informative one
  (`ACCEPTED`, `DEFINITELY_NOT_ACCEPTED`, or `RETIRED` are preserved).
- `RETIRED` — the candidate has durably lost selection authority (superseded
  by a winner, or explicitly retired). Retirement is permanent within a
  generation and never resurrected by a later observation.

**Artifact bindings** (observed remote PRs — repository, PR number, head
SHA, base SHA) are recorded separately from authority state as an
append-only, monotonically revisioned history per candidate. An unavailable
provider observation never erases a previously recorded binding, and a late
binding discovered after a candidate is retired is still recorded for
inspection but never restores selection authority.

## Selection

Selecting a winner is atomic and requires, in a single transaction:

1. The generation is `ACTIVE` (not retired).
2. No prior winner exists for this generation, or the same winner/PR is
   being re-submitted with an acceptance record that still exactly matches
   the generation and latest binding (idempotent repeat — no new adoption
   obligation is emitted). Matching only the stored winner pointer is not
   sufficient: a changed base, oracle fingerprint, or binding revision is
   denied as stale or mismatched.
3. No pending reconciliation from a different generation's uncertain merge
   outcome blocks new merge authorization (see below).
4. The candidate is not retired.
5. The caller-supplied acceptance record (repository, generation, candidate,
   PR repository/number, head SHA, base SHA, Issue-oracle fingerprint, and
   expected binding revision) matches the latest recorded, unretired binding
   for that candidate exactly. A stale acceptance record (an old head/base or
   an outdated revision) can never select a winner, even for a candidate
   that later does win with a fresh record.

At most one candidate-and-PR pair may be selected within a generation.
Selecting a winner durably retires every other candidate and its latest
binding in the same transaction (withdrawing their selection authority even
if their submission outcome was unknown or their PR was not yet observed),
and durably records:

- One `ADOPTION` obligation identifying the winning candidate/PR.
- One `ARTIFACT_CLEANUP` obligation per retired candidate, so a downstream
  consumer can stop the corresponding remote session.

A winner's identity is never changed or cleared to elect a different result
within the same generation.

## Aggregate Failure

One durable, generation-keyed `AGGREGATE_FAILURE` obligation may be recorded
when either:

- Every candidate has been explicitly exhausted (no candidate remains
  `NEVER_SUBMITTED`, `SUBMISSION_CLAIMED`, `ACCEPTED`, or
  `SUBMISSION_OUTCOME_UNKNOWN`) with no winner selected, or
- The selected winner's merge outcome is recorded as `DEFINITELY_FAILED`
  before merge.

Recording aggregate failure retires the generation (freeing the
single-active-generation slot). It is refused while any relevant
submission/merge outcome remains unknown, while an unselected candidate is
still eligible, or after the winner's merge outcome is recorded as `MERGED`.
Duplicate reports are idempotent and never create a second obligation.

## Generation Retirement and Reconciliation

An explicit source-Issue closure or Issue-oracle replacement can retire a
generation via `retire_generation` without classifying its individual
competitors as additional Issue failures — no candidate authority state is
changed by generation retirement. If the retired generation had a selected
winner whose merge outcome was still `UNKNOWN`, a pending reconciliation
record is durably kept: a **replacement generation may be created**, but it
cannot authorize its own merge (winner selection is denied) until that
reconciliation is resolved, either explicitly or by recording the original
winner's established merge outcome (`MERGED` or `DEFINITELY_FAILED`).

## Obligation Delivery Lifecycle

`ADOPTION`, `ARTIFACT_CLEANUP`, and `AGGREGATE_FAILURE` obligations move
through `PENDING` → `DELIVERED` → `ACKNOWLEDGED`. Both transitions are
row-level idempotent and restart-safe: a consumer that crashes after
producing the obligation's observable effect but before acknowledging it can
safely retry acknowledgement without creating a new generation or a new
obligation, and a consumer that has not yet observed a `PENDING`/`DELIVERED`
obligation can always re-list it after restart.

## Concurrency, Idempotency, and Durability

State transitions are durable and use optimistic locking, mirroring the
canonical blocker ledger and the durable repair allowance state machine:

- **Compare-and-Set (CAS):** every mutating call requires the caller's
  expected namespace epoch; stale writers are rejected.
- **Operation Idempotency:** mutations record their operation ID and a
  canonical hash of the payload in an operation journal. Replaying an
  identical operation ID with an identical payload returns the committed
  snapshot idempotently; conflicting reuse with an altered payload is
  rejected.
- **Fail-Closed Retained State:** corrupt or unreadable storage raises
  explicitly rather than returning a false empty/zero-success snapshot. A
  namespace that has simply never had a generation admitted reads back as an
  empty snapshot at epoch 0, which is not itself an error condition.

## Local Storage and Limits

The ledger is backed by a local SQLite database in Write-Ahead Logging (WAL)
mode, with immediate transactions and a busy timeout, independent from the
canonical blocker ledger's and repair allowance ledger's own database files.

**Limits and Non-Goals:**

- This module defines only the durable semantic model and public transition
  boundary. It does not send Jules HTTP requests, produce CI/verdict
  results, cancel remote sessions, expose public configuration, perform
  generic multi-provider model migration, garbage-collect retired
  identities, or reconcile state between independent controllers.
- It does not read or write `cloud.csv`, `implementation_slots.py`, or
  `attempt_manager.py` state. Existing single-session bindings in those
  systems are left entirely unchanged by this module; wiring production
  observations into these durable records is the responsibility of later
  stages.
- Distributed multi-host storage replication and persistent reconstruction
  after catastrophic local disk loss without backups are outside the scope
  of this local store.

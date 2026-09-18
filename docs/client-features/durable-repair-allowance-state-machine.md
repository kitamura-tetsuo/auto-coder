# Durable Repair Allowance State Machine

The durable repair allowance state machine models bounded automatic repair
allowances using durable corrective generations rather than review or commit
counts (GitHub Issue #2140, a stage of the convergent PR review tracking
family #2134). It consumes canonical blocker IDs persisted by the
[canonical PR blocker ledger](canonical-pr-blocker-ledger.md) (Issue #2135)
and accepted scope/evidence revisions; it does not itself perform review
matching, thread publication, or provider observation.

This module owns only the provider-independent durable state machine and
allowance policy. Exhaustion of a blocker's repair allowance never changes
its correctness verdict, resolves review threads, deletes obligations, or
authorizes merge — those remain owned by the canonical blocker ledger and by
downstream merge-decision logic.

## Namespace and Scope

Repair allowance state is keyed by the same namespace as the canonical
blocker ledger — canonical GitHub API origin, repository slug, and pull
request number — plus a canonical `blocker_id` from that ledger. Each
blocker's allowance is maintained independently of its semantic disposition:
an allowance exists, accrues failures, and can be exhausted regardless of
whether the blocker is currently `OPEN`, `VERIFIED_CORRECTION`, or
`RECURRENCE` in the blocker ledger.

A new, previously unseen blocker receives a default allowance of three
failed corrective generations. An explicitly supplied limit must be a
positive integer. Once captured (at first admission referencing that blocker,
or at an explicit operator grant), a blocker's active limit does not change
merely because global configuration or backend routing changes; only an
explicit operator grant (see below) can raise it.

## Corrective Generations, Not Commits or Reviews

A **corrective generation** is one controller-admitted logical repair
handoff: a unique generation ID, an immutable bundle reference, the covered
blocker/scope revisions, an owning provider/task or local invocation
identity, an admission epoch, a delivery operation identity, and an observed
baseline. Transport retries, individual commits, review executions, root
comments, and repeated observations of the same event are never new
corrective generations — they are recorded as history against the one
generation they belong to.

At most one corrective generation may be outstanding (not yet `SETTLED` or
`SUPERSEDED`) for a given pull request at a time. Concurrent admission
attempts contend for that single slot via compare-and-set on the namespace's
epoch counter; only one wins, and the loser is rejected rather than silently
admitted.

### Lifecycle

Each generation is represented by exactly one of seven distinct states:

- `RESERVED` — admitted, delivery not yet confirmed. A proven **definite
  non-delivery** (for example, a proven provider quota refusal before any
  corrective work reached the provider) returns the generation to `RESERVED`
  so the same logical generation may retry delivery; it never charges a
  failure and never allocates a new generation.
- `CONFIRMED_DELIVERED` — the provider/task confirmed receipt of the
  corrective work.
- `INDETERMINATE` — the delivery outcome could not be confirmed (for
  example, the send response was lost). An indeterminate generation remains
  pending reconciliation and continues to hold the single-outstanding-slot;
  it never authorizes another speculative generation on its own. Only an
  explicit `supersede_generation` reconciliation call — never a fresh
  admission attempt — can release the slot.
- `PENDING_COMPLETION` — delivery confirmed, but completion evidence is
  unavailable so far. Unavailable completion evidence preserves this state.
- `PENDING_REVALIDATION` — genuine, causally-later completion evidence has
  been recorded (with a caller-supplied monotonic completion marker); the
  generation now awaits independent per-blocker validation. A completion
  observation that predates the generation's admission (belongs to the
  pre-repair baseline) cannot advance a generation past `PENDING_COMPLETION`,
  and a head/commit change alone is never treated as completion evidence.
- `SETTLED` — every blocker covered by the generation has a determined
  settlement (`CORRECTED` or `STILL_OPEN`).
- `SUPERSEDED` — an operator explicitly reconciled a stuck `INDETERMINATE`
  generation without charging any covered blocker.

## Charging a Failure

A covered blocker's failed count is incremented **at most once per
generation**, and only once all of the following hold:

1. The generation reached confirmed delivery of actual corrective work.
2. The generation's completion evidence is causally bound to (at or after)
   that confirmed delivery — an arbitrary head change without recorded
   completion evidence never counts.
3. An independently accepted validation, captured at or after that
   completion evidence, demonstrates the blocker remains unmet.

A validation captured before completion evidence is recorded but held
pending; it cannot retroactively combine with completion evidence recorded
afterward. Once a blocker is settled for a generation, replaying the same or
a newly-visible duplicate terminal record never charges it again. A
completed generation that produced no code change can still be charged, but
only given genuinely later, independent validation evidence — the absence of
a code change is not itself a reason to withhold or force a charge.

When one generation covers several blockers, each is settled against its own
independent disposition. A blocker with insufficient proof remains `PENDING`
(neither resolved, failed, nor silently omitted), and progress on one
covered blocker never resets another's count.

## Exhaustion

A blocker's status becomes `EXHAUSTED` once its failed count reaches its
captured limit. While any currently open blocker in a pull request is
`EXHAUSTED`, admission of **every** new automatic corrective generation for
that PR is denied — including a bundle that covers only a different,
newly-found blocker. Exhaustion never changes a blocker's correctness
verdict, resolves threads, deletes obligations, or authorizes merge on its
own.

Failure history and remaining allowance survive changed wording, new
comments, new heads, partial fixes, category correction, model/provider
changes, process restart, and PR close/reopen. A blocker that is
demonstrably fully corrected no longer requires repair, but its history is
not erased; a later genuine recurrence under the same blocker ID retains its
prior allowance state until an explicit new grant is accepted.

## Explicit Operator Grants

An operator grant is a distinct transition from validation, semantic
closure, and provider selection. It is valid only when the PR has no
outstanding or indeterminate generation. `target_blocker_ids` of `None`
selects every currently `EXHAUSTED` or `RECONCILIATION_REQUIRED` open
blocker known to the namespace; an explicit list grants only those. A grant
captures a new bounded limit for the granted blockers while preserving their
full prior failure history (the historical total is never claimed to have
been zero). A grant is idempotent by request ID: identical replay returns
the original result, conflicting reuse of a request ID with a different
payload is rejected, a stale expected epoch is rejected, and a grant
attempted while any generation is outstanding or indeterminate is rejected.
A grant never itself sends a repair request or authorizes merge.

## Concurrency, Idempotency, and Durability

State transitions are durable and use optimistic locking, mirroring the
canonical blocker ledger:

- **Compare-and-Set (CAS):** Every mutating call requires the caller's
  expected namespace epoch. Stale writers are rejected.
- **Operation Idempotency:** Mutations record their operation ID and a
  canonical hash of the payload in an operation journal. Replaying an
  identical operation ID with an identical payload returns the committed
  snapshot idempotently; conflicting reuse with an altered payload is
  rejected.
- **Fail-Closed Retained State:** Corrupt or unreadable storage raises
  explicitly rather than returning a false empty/zero-success snapshot.
- **Ambiguous or Pre-Upgrade History:** A blocker referenced by recorded
  generation history but lacking a corresponding allowance record is
  represented as `RECONCILIATION_REQUIRED`, never as a fresh zero-failure
  allowance.

## Local Storage and Limits

The repair allowance state machine is backed by a local SQLite database in
Write-Ahead Logging (WAL) mode, with immediate transactions and a busy
timeout, independent from the canonical blocker ledger's own database file.

**Limits and Non-Goals:**

- This module defines only the durable semantic model and public transition
  boundary. It does not observe production providers, send repair requests,
  resolve GitHub threads, or authorize merges; those remain the
  responsibility of downstream integration.
- It does not reproduce or replace the existing, separate
  `MAX_ADVERSARIAL_VALIDATIONS` review-count gate in `_handle_pr_merge`, nor
  the existing adversarial-validation backend-quota `EXHAUSTED` verdict
  described in
  [pr-adversarial-validation-backend-exhaustion-exhausted.md](pr-adversarial-validation-backend-exhaustion-exhausted.md).
  Those are distinct, pre-existing mechanisms that this module leaves
  unchanged.
- Distributed multi-host storage replication and persistent reconstruction
  after catastrophic local disk loss without backups are outside the scope
  of this local store.

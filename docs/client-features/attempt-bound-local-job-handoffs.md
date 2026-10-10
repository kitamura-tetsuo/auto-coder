# Attempt-bound local job handoffs

`LocalJobStore` is the durable evidence boundary between upstream admission and
a future local worker. It supports only Issue implementation and explicit-local
PR review-correction jobs. The producer does not invoke a model, prepare a
workspace, publish a branch, or settle either upstream domain lifecycle.

An Issue offer must match a current `IssueDispatchGuard` attempt claim. A PR
offer must match both the current `LocalReviewRepairStore` claim and its exact
outstanding `RepairAllowanceLedger` generation. Repository, target number,
upstream attempt or generation, selected backend, immutable SHA-256 input
identity, and reconstructible invocation input are committed together. Replays
of the same origin return the same job, while changed input or authority fails
closed rather than overwriting it.

For Issues, only the dispatch guard's internal pre-adapter `pending` claim is
admission authority. A finalized `INDETERMINATE` observation may represent an
invocation that entered its backend and therefore cannot authorize a new job,
even though the guard's general inspection API intentionally presents both
states as indeterminate to existing consumers. Unreadable PR repair or
allowance evidence likewise refuses the offer without modifying either owner.

Acceptance is an ownership transfer, not a snapshot followed by an unrelated
write. The job database attaches the relevant upstream SQLite database(s),
conditionally changes the exact still-current pre-entry owner, and inserts the
job in the same transaction. Issue dispatch checkpoints `invoking` immediately
before calling its adapter; PR repair checkpoints backend entry separately from
its long-lived `executing` phase. Whichever transition wins prevents the other,
so a released/replaced owner or an already-entered invocation cannot leave a
claimable job behind.

Jobs move through separate `pending`, `running`, `result_recorded`,
`downstream_effects_pending`, and `settled` states. Only a pending job is
claimable. Claim acquisition creates a random execution incarnation in one
SQLite transaction; every result and later transition compares that
incarnation and its expected state. Consequently, contention has one winner
and a stale runner cannot replace the winner's evidence.

A result records one of `completed`, `cannot_fix`, `failed`, or `interrupted`.
Before that checkpoint, the store persists the output as an artifact bound to
the exact job, execution incarnation, and outcome. `record_result` verifies
that binding transactionally; arbitrary paths, missing artifacts, and another
job's artifact cannot authorize downstream work. An invocation result is not
domain completion. Downstream work receives its own pending checkpoint, and
only its authorized consumer may mark the envelope settled.

After restart, `discover_unsettled` exposes every nonterminal record. Pending
means the backend was positively not entered and can receive its first claim;
running means the invocation may have started and remains suppressing until an
authoritative reconciler records its result. Store corruption or write failure
returns no acceptance or transition and does not mutate Issue dispatch, PR
repair, allowance, or implementation ownership.

Regression coverage is in `tests/test_local_job_handoff.py`, including real
Issue-dispatch and PR-repair/allowance producer origins, reopen durability,
SQLite contention, stale-writer fencing, exact-attempt isolation, missing-result
evidence, and restart suppression.

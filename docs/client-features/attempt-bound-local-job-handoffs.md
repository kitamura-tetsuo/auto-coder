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

Jobs move through separate `pending`, `running`, `result_recorded`,
`downstream_effects_pending`, and `settled` states. Only a pending job is
claimable. Claim acquisition creates a random execution incarnation in one
SQLite transaction; every result and later transition compares that
incarnation and its expected state. Consequently, contention has one winner
and a stale runner cannot replace the winner's evidence.

A result records one of `completed`, `cannot_fix`, `failed`, or `interrupted`
and requires a durable result/output reference. An invocation result is not
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

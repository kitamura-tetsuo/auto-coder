# Implementation-ready Issue dispatch gate

This gate is the implementation-ready specification quality contract from
Issue #1730.  It is one shared production boundary rather than an optional
planning phase: standalone submissions require a current individual `READY`,
and parent submissions require both a current decomposition `READY` and a
current individual `READY` for the sequentially selected direct child.  No
parent Requirement is inherited by a child. `BLOCKED` withdraws the reviewed
Issue's explicit readiness label and requires explicit resubmission for that
label; an inherited child result preserves parent readiness. Operational `ERROR`
preserves readiness for retry.  These checks cover daemon, explicit/urgent, local, and
cloud dispatch and precede implementation ownership and capacity admission.
They do not create `plan.md`/`tasks.md`, allocate implementation slots, relax
quota or routing policy, or permit sibling implementation in parallel.

Before classifying or snapshotting any readiness submission, Auto-Coder examines
every line-form `Parent-Issue`, `Parent_Issue`, or `Parent Issue` declaration,
rejects malformed, ambiguous, self-referential, contradictory, missing, PR, and
structurally rejected targets, and materializes one valid same-repository target
as the native GitHub parent. Transport, authorization, rate-limit, and service
failures remain retryable operational failures. The Issue and its native graph
are fetched again after reconciliation; body metadata is never used as a second
parenthood oracle by validation or implementation. Every authoritative direct
child, including closed children, is reconciled before a parent-set identity is
created. Explicit processing observes the same creation-anchored one-minute
stabilization window as webhook and startup recovery, and changed identities can
be validated while existing implementation ownership remains intact.

An Issue can enter implementation only while its current authoritative GitHub
snapshot contains the `implementation-ready` label. Auto-Coder checks this
cache-bypassing snapshot at the shared pre-dispatch boundary before reserving an
implementation slot, adding ownership, creating a branch, or starting any local
or cloud backend. The rule also applies to explicit `--only` and forced runs;
removing the label before dispatch makes the Issue ineligible again.

Semantic validation decisions are bound to the repository and Issue number, an
exact SHA-256 digest of the authoritative title and body, and a policy identity
covering the analyzer prompt, structured finding contract, provider route, and
model. Completed `READY` and `BLOCKED` decisions are durable across restarts;
transport or malformed-output `ERROR` decisions are never cached. Validation is
coalesced per identity and happens before implementation capacity is acquired.
Immediately before dispatch, Auto-Coder re-fetches the Issue and rejects stale
`READY` evidence if its text changed or readiness was withdrawn.

A current `BLOCKED` decision independently denies dispatch even if its GitHub
updates fail. Before publishing its single identity-marked actionable summary
and before removing `implementation-ready`, Auto-Coder re-fetches and compares
the generation. Consequently a late result cannot modify edited Issue text, and
an edit after label removal is not silently resubmitted; a user must explicitly
add `implementation-ready` again. Validator `ERROR` preserves the label and
publishes no specification-defect claim so a later attempt can retry.

When the submitted Issue has direct children, its `implementation-ready` label
submits the exact parent/direct-child specification set instead of authorizing a
standalone parent implementation. Auto-Coder binds decomposition decisions to
the repository and parent, stable direct-child membership, exact parent and child
title/body digests, and the effective decomposition prompt, result contract, and
provider/model route. Closed children remain generation members, so completing a
sibling does not invalidate a reusable durable `READY` decision.

Container parents are coordination-only specification sets and never enter an
implementation backend, including explicit, forced, urgent, local, cloud, and
recovery routes. Once the exact current decomposition and every exact current
child have reusable `READY` evidence and every direct child is closed, a final
cache-bypassing reconciliation closes the still-submitted parent directly. Any
identity or membership drift, reopened child, validation `ERROR`, or GitHub close
failure leaves completion observable and retryable without implementation fallback.
Parent relationships may be established only against an open same-repository
Issue, and an Issue that is already a child or already has children cannot be
made the middle of a nested hierarchy. Pre-existing nested hierarchies fail
closed until manually corrected.

Set and direct-child validation jobs are eagerly coalesced by exact identity and run
concurrently under one configurable positive `process_issues.validation_concurrency`
bound (default 2), independently of implementation capacity. Standalone validation
uses the same global bound. Validation completion order never changes sequential
sibling implementation order. A current set `BLOCKED` result removes readiness from the parent and
publishes one identity-marked set summary only after a fresh generation check;
`ERROR` preserves the submission and is retried. Once the set is `READY`, the
first eligible child in the existing sequential sibling order receives its own
exact-generation specification validation without requiring or receiving a
child `implementation-ready` label. Child `BLOCKED` withdraws any explicit
readiness label on that child while preserving the parent submission. The child
withdrawal requires fresh specification and family checks and retries durably
on failure; the withdrawal effect completes when the child label is absent. Child `ERROR` preserves readiness. Immediately
before ownership and dispatch, both the live parent set and child identities are
re-fetched, and parent-label withdrawal, membership edits, or specification edits
fail closed.

Adversarial PR validation has a separate, instance-local
`process_issues.adversarial_validation_concurrency` bound (default 2; set it to 1
for serialized execution). Capacity is acquired only after CI, review, and prior-result
eligibility gates pass. It remains owned through isolated-worktree cleanup, durable
attempt finalization, publication, and merge authority checks. Duplicate local triggers
for one PR are coalesced, while durable attempt sequence fencing prevents an older
same-head result from gaining authority after any newer attempt starts.

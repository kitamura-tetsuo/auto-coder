# Forced adversarial-validation recovery

An explicit `--only <PR> --force` run starts a new adversarial-validation
attempt for the current head even when that head already has a verdict or the
normal review limit has been reached. Ordinary scheduling and non-forced
`--only` runs retain same-head and review-limit suppression. Every attempt has
a durable, start-ordered identity; prior reviews remain immutable, overlapping
attempts remain distinct, and a late older completion cannot supersede the
newer applicable verdict. Force changes retry eligibility only and does not
relax validator response, requirement coverage, or review-anchor checks.
Successfully recovered or irrelevant changed-file evidence is retained for the
exact reviewed head. An unchanged-head retry reuses that classification, while
a new head invalidates it. Unavailable correctness-relevant evidence must be
scoped to dependent `UNVERIFIED` requirements and an irreducible recovery gap;
contradictory `PASS` or `VERIFIED` claims are rejected without first synthesizing
a different invalid verdict.

## Force reaches validation past the unresolved-thread gate

Before this attempt can start, two earlier pre-validation gates independently
check for unresolved GitHub review threads: the initial unresolved-thread gate
(evaluated before merge/validation is attempted at all) and a recheck of
threads performed after a completed Codex GitHub review. Both gates now also
consult `--force`: a known unresolved thread, whether it is an authentic
Auto-Coder adversarial-validator finding, an unrelated (human or other-bot)
thread, or a mixture of both, no longer suppresses the forced attempt at
either gate. This closes the case where an admitted `--only <PR> --force` run
reported "unresolved review threads" or "repair already requested" and never
reached a fresh validator invocation, because the current head already had a
saved verdict (including `ERROR`) so the older-head exception below did not
apply.

This is strictly an *admission* change. Every unresolved thread that
authentically originates from Auto-Coder's adversarial reviewer (root heading
`### Auto-Coder adversarial finding` or `### Auto-Coder material test-oracle
gap`, authored by the configured reviewer App identity) is still supplied to
the forced validation attempt for independent per-thread disposition, exactly
as for the pre-existing older-head exception -- but tagged as an explicit
forced same-head revalidation (`### Forced adversarial-validation
revalidation (explicit --force)` in the prompt), never as evidence that the
head changed or that an implementer replied. A thread only closes when the
validator's disposition for that exact thread is `ADDRESSED` with rationale
and evidence, its independent explanation is recorded, and its resolution is
confirmed; every other unresolved thread -- including one that copies the
Auto-Coder heading without matching author identity -- remains open and keeps
blocking merge.

Because admission and merge authorization are kept separate, unresolved
threads that force skipped past at admission (or a new thread that appears
while validation is running) still block automatic merge: after adversarial
validation completes, a fresh authoritative review-thread read runs again
immediately before merge, independent of whatever thread state admitted
validation. Any remaining blocking thread at that point -- including one that
neither `--force` nor the validator's dispositions ever authorized to close --
prevents the merge.

Prior repair-request delivery (or its already-requested/delivered outcome) is
informational only: it never counts as independent review, and it never
suppresses the forced attempt. Force does not create a new repair-delivery
obligation and does not resend feedback whose delivery is already durably
confirmed.

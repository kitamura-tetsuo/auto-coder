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

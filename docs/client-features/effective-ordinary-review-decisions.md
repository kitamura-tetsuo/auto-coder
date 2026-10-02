# Effective ordinary-review decisions

`effective_review_decision.py` derives one side-effect-free decision from the
parsed ordinary validation result and the complete accepted-Strong-finding
projection. The decision retains its target, validation-attempt, source,
finding-set, and association revisions so a consumer can reject stale output.
The raw validator result and requirement coverage remain separate diagnostic
evidence.

Operational `ERROR`, `EXHAUSTED`, `BLOCKED`, and `INCONCLUSIVE` outcomes wait
without dispatching a repair. Incomplete, ambiguous, stale, or insufficiently
adjudicated accepted-finding evidence requires reconciliation. An exact
independent `ADDRESSED` proposal requests lifecycle-owner closure acceptance;
the policy does not close findings itself.

Current independently upheld accepted implementation findings yield
`NEEDS_FIX`; accepted regression/test-oracle findings yield `NEEDS_TESTS`.
Mixed findings use `NEEDS_FIX` while retaining every original correction and
category exactly once by Strong source identity. Only accepted `FIXED` or
`INVALID` lifecycle state removes that obligation. With complete verified (or
justified irrelevant) coverage and no remaining accepted, specification,
provenance, or operational blocker, `PASS` is approval-eligible. This does not
bypass Strong-completion, CI, or other final merge gates.

The policy performs no GitHub/provider calls and mutates neither the accepted
finding lifecycle nor the blocker ledger. Consequently this policy-only stage
is observability-neutral: it introduces no processing origin, durable resume
path, structured dashboard event, or production consumer; consumer trace and
dashboard wiring belong to the subsequent integration stage.

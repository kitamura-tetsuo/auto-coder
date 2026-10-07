# Effective ordinary-review decisions

`effective_review_decision.py` derives one side-effect-free decision from the
parsed ordinary validation result and the complete accepted-Strong-finding
projection. The decision retains its target, validation-attempt, source,
finding-set, and association revisions so a consumer can reject stale output.
The binding also carries the PR reopen epoch, while each retained accepted
record carries its own source and canonical-association revisions.
The raw validator result and requirement coverage remain separate diagnostic
evidence.

A normalized ordinary `NEEDS_FIX` result may dispatch implementation repair
when its coverage includes `VIOLATED` requirements with matching concrete
findings. Such a violation is adjudicated evidence for correction, not missing
evidence and never approval clearance. Unverified requirements, violations
without matching findings, specification gaps, and unexplained changes still
require reconciliation. `PASS` continues to require only `VERIFIED` or justified
`IRRELEVANT` entries.

Operational `ERROR`, `EXHAUSTED`, `BLOCKED`, and `INCONCLUSIVE` outcomes wait
without dispatching a repair while retaining known accepted obligations for a
later invocation. Incomplete, ambiguous, stale, or insufficiently
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
finding lifecycle nor the blocker ledger. Its production consumer, durable
retention, pre-send approval authority, corrective handoff and closure path are
documented in `effective-review-decision-application.md`.

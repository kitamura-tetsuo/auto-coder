# Effective ordinary-review decisions

Published BLOCKED results with retained actionable ordinary evidence resume
correction before forced reviewer admission. Recovery requires the producing
attempt and current review identities to match, keeps addressed findings in
pending closure, and does not confer approval or closure authority.

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
later invocation. Incomplete, ambiguous or stale accepted-finding authority
requires reconciliation before any repair. A complete historical OPEN record
that only lacks current-target adjudication is retained separately in
`pending_reconciliation`; it blocks approval without suppressing an independently
established current correction. An exact
independent `ADDRESSED` proposal requests lifecycle-owner closure acceptance;
the policy does not close findings itself.

Pending closure acceptance and historical adjudication do not suppress
independently upheld repairs or new demonstrated ordinary violations with complete
requirement coverage. The decision separates current accepted `corrections`,
`ordinary_findings` and `ordinary_gaps`, exact `pending_closures`, and historical
`pending_reconciliation`. Only the current actionable sets reach the repair owner.
Pending closures and adjudication still block
approval and remain OPEN until their lifecycle owner accepts them. Unavailable
or ambiguous authority and unverified ordinary evidence still require
reconciliation rather than speculative repair.

An ordinary status label does not establish a correction. Implementation
findings require a demonstrated counterexample and matching `VIOLATED` coverage;
focused test gaps require material regression evidence for a covered explicit
requirement. Specification gaps, unexplained changes, and unsubstantiated coverage
cannot authorize an ordinary correction. Existing directly current accepted
findings retain their independent repair authority, without promoting incomplete
ordinary evidence into the same request. Accepted source, blocker and TOG identities
are adjudicated through their accepted records: a restated historical counterexample
cannot manufacture a new ordinary correction. A valid current implementation
finding takes priority over a test-only route while preserving both actionable
sets. Unsupported verdicts remain non-authorizing.

If no current correction is actionable, historical obligations yield reconciliation
wait and exact closure proposals yield closure-acceptance wait. Neither set is
represented as a request to repair the historical counterexample. Native thread
resolution does not close the accepted lifecycle or manufacture a closure proposal.

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

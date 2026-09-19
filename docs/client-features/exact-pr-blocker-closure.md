# Exact PR Blocker Closure Validation

Exact PR blocker closure validation guarantees that review blockers and GitHub review
threads are resolved only when current evidence demonstrably fixes that exact
blocker's complete recorded scope on the active PR head (GitHub Issue #2138, Stage S4
of the convergent PR review tracking family #2134).

## Exact Blocker Identity & Concrete Scope Binding

When evaluating candidate closures for claimed review threads:
- **Authenticated Scope Evaluation:** The controller evaluates closure candidates
  against the authenticated original review finding and the canonical blocker's accepted
  concrete correction scope in the PR-scoped Canonical PR Blocker Ledger.
- **Identity Integrity:** Candidate evaluation identifies the canonical GitHub API origin,
  target repository, PR number, blocker ID, owned concern IDs, thread root comments,
  requirement manifest revision, evaluated head/base SHAs, and ledger revision.
- **Anti-Substitution:** Dispositions cannot substitute a similarly worded finding, an
  array position, a shared requirement ID, or a different thread's finding.
- **Mismatched Identity Rejection:** Unknown, duplicate, cross-target, or mismatched
  blocker identities in dispositions are rejected fail-closed before mutating ledger
  state or GitHub threads.

## Authoritative Boundary & Concern Coverage Validation

- **Authoritative Boundary Verification:** Evidence and rationales must address the
  specific component, module, or production boundary recorded in the blocker's
  `authoritative_boundary` (e.g. `dashboard_detail.py`).
- **Wrong Rationale Rejection:** Evidence citing a different boundary or finding (such as
  citing an overview history link in `dashboard.py` for a foreign-reference/count finding
  in `dashboard_detail.py`, as observed in #2132) cannot authorize closure. Such
  mismatches evaluate to `STILL_VALID` or `INCONCLUSIVE`, never `ADDRESSED`.
- **Complete Concern Coverage:** A canonical blocker may own multiple concrete concerns
  (manifestations) within its accepted scope. `ADDRESSED` is accepted only when current
  evidence establishes correction of every concrete concern owned by that blocker.
- **Partial Corrections Identified:** If only a subset of concerns is fixed while
  another remains reproducible, the blocker remains open with the remaining concerns
  explicitly identified, and the thread is not resolved.
- **Scope Invariance Against New Defects:** Fully correcting an original blocker
  closes that blocker. Introducing a different defect under the same requirement on a
  different path tracks the new defect separately rather than retroactively enlarging
  the closed blocker's scope.

## Producer Contract Verification & Category Distinction

- **Producer Path Verification:** Implementer assertions, newly pushed commits,
  GitHub `outdated` flags, relocated lines, test names, source-text matches, or green CI
  results are insufficient by themselves. A boundary-dependent correction requires
  evidence establishing that the supported production origin creates the required
  precondition and preserves it across consumer boundaries.
- **Absent API Rejection:** Evidence relying on an API, field, or method absent from the
  current producer cannot establish correction. Passing helper tests that invoke
  unconnected internal functions while the real consumer contract remains broken do not
  permit closure.
- **Implementation vs Test-Oracle Distinction:** Production defects and test-oracle gaps
  remain distinct. A production code fix may be accepted and closed while an independently
  valid regression test gap remains open. Conversely, a test-oracle gap cannot be closed
  merely because a production code change or passing helper test occurred.
- **Requirement Boundary:** No closure may acquire an obligation from Acceptance Scenarios
  or reviewer comments that the explicit Issue Requirements do not support.

## Multi-Blocker Compound Root Resolution Gating

- **Full Obligation Resolution Required:** A single GitHub review root comment or thread
  may own multiple canonical blockers (compound root). The GitHub review thread is
  resolved only when *all* blockers owned by that root/thread have valid current closure
  (`VERIFIED_CORRECTION` or `AUTHORIZED_INVALIDATION`).
- **Compound Root Independence:** Verifying and closing one canonical blocker owned by a
  compound root records its closure in the ledger but leaves the compound GitHub thread
  unresolved while another owned blocker remains open.
- **Alias Root Resolution:** An alias root comment that owns only the corrected blocker
  can be resolved independently once its owned obligation is closed.

## Durable Acceptance Before Effect & CAS Fencing

- **Durable Acceptance Before Effect:** An accepted closure must be durably recorded in
  the Canonical PR Blocker Ledger (as a transition to `VERIFIED_CORRECTION` with evaluated
  head/base, manifest revision, and expected ledger revision) *before* any resolve mutation
  is sent to GitHub.
- **Durable Acceptance Failure:** If writing the closure transition to the ledger fails
  (database unavailable, permission error, or I/O error), no resolution mutation is
  issued to GitHub.
- **CAS and Staleness Fencing:** The controller rechecks the authoritative PR head SHA and
  ledger revision immediately before the resolve mutation. If the head advanced or the
  ledger revision is stale, the resolve mutation is suppressed.
- **Stale Resolution Rollback:** If the PR head advances during a resolve mutation, the
  controller attempts to unresolve the thread with bounded retries, marks the thread with
  a durable stale-resolution blocker marker on GitHub, records it in the stale review
  thread registry, and blocks merging until reconciled.
- **Startup Reconciliation:** On process restarts or crashes after committed ledger
  acceptance but before GitHub mutation completion, exact-operation reconciliation recovers
  without treating an unverified thread flag as proof of correction.

## PR-Level Result Independence

- **Independent Blocker Lifecycle:** Per-blocker closure is independent of the aggregate
  PR verdict. A valid blocker correction is persisted and its dedicated thread resolved
  even if the PR overall evaluates to `NEEDS_FIX`, `NEEDS_TESTS`, or encounters an
  operational error.
- **No Blanket Waiver:** Receiving an aggregate PR-level `PASS` or exhausting repair
  retries does not bypass or close unresolved owned review obligations.

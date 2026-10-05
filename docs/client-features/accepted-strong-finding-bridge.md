# Accepted Strong finding bridge for ordinary rereview

Findings durably accepted by the Strong-audit result-acceptance path remain
*known findings* for ordinary PR rereview, independently of reviewer-session
identity (`src/auto_coder/accepted_finding_bridge.py`).

## Authorities

* `PrReviewCycleRepository` remains the semantic owner of Strong rounds,
  findings and their FIXED/INVALID lifecycle. The bridge never closes,
  reopens or edits a finding there.
* `CanonicalPRBlockerLedger` remains the owner of blocker identity and native
  root aliases. The bridge adds only a source alias, an ordinary-TOG alias and
  authenticated root aliases, and projects accepted FIXED/INVALID state into
  `VERIFIED_CORRECTION` / `AUTHORIZED_INVALIDATION`.
* Ordinary reviewer sessions are not a prerequisite: representations are
  re-derived from the two stores on every read and are never persisted into a
  reviewer-session checkpoint.

## Identity

* Source identity is `<accepted round_id>:<finding_id>`; only findings whose
  origin is a Strong claim recorded in that round (`finding_ids`) qualify.
  Unaccepted model output, implementation-agent claims and arbitrary comments
  create no authority.
* Each source identity maps to exactly one canonical blocker (reused, never
  reallocated). Distinct findings never collapse, even with a shared
  requirement or path.
* The ordinary TOG label is `TOG-ACCEPTED-<hash of source identity>`; it is a
  representation, registered as a `test_oracle_gap` alias so existing TOG
  reconciliation resolves to the existing blocker and root instead of
  publishing a duplicate. It is only produced once the canonical identity is
  established; otherwise the obligation is reported as unavailable.
* A native root is associated only from an exact, authenticated publication
  marker (`auto-coder-two-tier-finding:v1:<payload identity>:<sha256(finding_id)>`)
  observed through `GitHubAppReviewer.observe_authenticated_review_roots`
  (or an eligible-author claimed thread). Lookalike or unauthenticated roots
  are ignored with a diagnostic; unmapped roots stay `UNKNOWN`/`AMBIGUOUS`.

## Ordinary rereview

`run_adversarial_validation` calls the bridge before prompt assembly. Unresolved
current-binding regression-gap findings are added to the lifecycle snapshot as
known gaps (even with no/new/changed reviewer session), and every accepted
finding with its qualified requirements and original scope is rendered into the
prompt. A model RESOLVED/ADDRESSED entry, a paraphrase or an omission cannot
close or rewrite a known finding (`accepted_open_gap_ids` in
`_reconcile_test_oracle_gap_lifecycle`). With no accepted-source association,
new-gap admission is unchanged. Ordinary STILL_VALID dispositions with evidence
are recorded as observations; INCONCLUSIVE/malformed ones retain the unresolved
obligation; ADDRESSED is only a closure proposal.

## Projection

`AcceptedFindingBridge.project()` returns an `AcceptedFindingProjection`
(source-to-canonical identities, categories/scopes, accepted state, current
observations, target binding, source/association revisions, diagnostics and an
explicit `complete` flag). It is complete only after a successful source read,
canonical join, and a source-revision re-check; contested compare-and-set
writes retry from fresh reads, failed writes and cross-store disagreement stay
incomplete, and an unreadable source is never reported as an empty set. Head
movement alone only marks evidence `HISTORICAL`; a differing asserted
contract/policy or a prior reopen epoch requires reconciliation. After Strong
publication is acknowledged the bridge also runs once (best effort) to retain
identities and roots; affected PRs are reconstructed idempotently on the next
ordinary read.

Explicit requirement references retain their qualified `#<issue>/REQ-NNN` identity in accepted gap representations. A root or thread can have independently retained non-Strong owners, including historical imports. Ordinary dispositions resolve against the accepted Strong source aliases: one accepted source is unambiguous, while multiple accepted sources still require reconciliation. The bridge leaves every other owner and its disposition intact. An already accepted exact closure remains authoritative when the root is reobserved.

## Closure-pending findings are not current-head violations

A retained OPEN accepted regression gap records a lifecycle obligation owned by
the review cycle; its original "tests are missing" prose is historical evidence
attributed to the originating head. Explicit-deliverable normalization
(`_normalize_findings_and_gaps`) therefore never promotes an accepted gap into a
VIOLATED implementation finding merely because it is still OPEN. Promotion
requires this attempt's own independent `STILL_VALID` thread disposition with
rationale and evidence for that exact gap (`context.accepted_upheld_gap_ids`).
Consequences:

- Addressed or unobserved: the attempt's coverage stays as the model judged it
  (for example VERIFIED), no missing-deliverable counterexample is synthesized,
  and `settle_accepted_gaps` leaves the accepted obligation to the effective
  decision, which is a nonapproving closure wait. The same retained evidence
  keeps `raw_ordinary_clear` true, so same-attempt closure acceptance stays
  reachable; once the owning cycle accepts it, the decision re-derives to PASS.
- Upheld by an independent disposition: the explicit missing deliverable remains
  a genuine NEEDS_FIX violation under the original correction identity.
- A different finding under the same Requirement keeps the Requirement VIOLATED,
  and a lexical overlap with it never drops the pending accepted gap, which only
  collapses into a finding carrying its own identity.
- A missing-test concern under a runtime-only Requirement stays a test gap.

Observability-neutral: no trace emission, admission gate, outcome or event
schema changed; only which already-existing result is derived changed, so
`docs/dashboard-observability.md` needs no update.

### Association, routing and recovery details

- **Native-root association.** A published Strong root carries no ordinary gap prose, so an
  independent `STILL_VALID` disposition is associated with its accepted gap through the exact
  native root (`record.root_comment_ids`) as well as through the gap prose of the thread. A root
  shared by several accepted gaps is ambiguous and upholds none of them.
- **Implementation routing under the original identity.** An upheld explicit deliverable becomes an
  implementation finding that keeps the gap identity: the effective decision is `NEEDS_FIX` /
  `IMPLEMENTATION_REPAIR` (not a focused test repair), the gap is not carried a second time, and
  publication reconciliation matches the finding to the original blocker (and its existing root)
  through the `test_oracle_gap` alias while preserving the blocker's authoritative boundary, so no
  new blocker, root or allowance is created.
- **Legacy saved results.** A saved `NEEDS_FIX`/`NEEDS_TESTS` headline for the current head replays
  as a repair only when a retained effective decision at that head covers every outstanding
  accepted finding (that decision is derived from current-target evidence). Otherwise the saved
  headline is only history: normal processing admits one fresh combined ordinary review, with no
  dummy commit, store reset or `--force`, and never sends the historical report as a repair.

With the optional Strong tier still configured and its exact audited H/B/M/P
binding unchanged, the accepted Strong finding bundle itself authorizes the
initial correction handoff before ordinary reviewer admission. This takes
priority over legacy saved-headline revalidation: a saved PASS or unbacked
non-PASS cannot cause another review of the unchanged audited head before a
correction has completed. The original source/canonical identities are retained
without creating an ordinary verdict or attempt. On a repaired head, or after
observed same-head completion, normal combined rereview and closure apply.

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

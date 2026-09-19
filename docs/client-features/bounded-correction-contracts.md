# Bounded Correction Contract Preservation

Bounded correction contract preservation guarantees that the exact correction scope,
authoritative requirements, and production-boundary regression oracles established
during adversarial review are carried immutably across repair handoffs and incremental
rereviews (GitHub Issue #2139, Stage S5 of the convergent PR review tracking family #2134).
It replaces unstructured concatenations of historical review comments with structured,
hash-identified repair bundles constructed from the PR-scoped Canonical PR Blocker Ledger.

## Canonical Blocker Snapshot as Sole Repair & Rereview Input

Repair and rereview inputs are built strictly from the canonical blocker snapshot in the
Canonical PR Blocker Ledger:
- **Controller-Owned Blocker Identity:** Every included blocker retains its persistent
  identifier (e.g. `blk_<hex>`), decoupled from comment and thread IDs.
- **Qualified Requirement References & Authoritative Text:** Each blocker explicitly cites
  its qualified Issue requirement (`Issue #<num>`, `REQ-<id>`) and authoritative requirement
  text from the validated manifest.
- **Accepted Original Correction Scope & Concern IDs:** The immutable scope description and
  constituent concern IDs accepted at initial admission are preserved across all iterations.
- **Production-Boundary Oracle Information:** Retains the authoritative boundary, required
  observable outcome, and regression oracle (evidence needed and minimal plausible incorrect
  counterexample).
- **Head & Manifest Binding:** Every blocker records the reviewed head SHA and requirement
  manifest revision bound to its current evidence.

## Verbatim Objective Preservation & Sole Implementation Contract

- **Objective Preservation:** The contributing Issue's short Objective is preserved verbatim
  as specification-scope evidence. It is never edited, replaced, or expanded into implementation
  obligations.
- **Requirements as Sole Contract:** The explicit `## Requirements` manifest is the sole
  merge-blocking implementation contract.
- **Non-Authoritative Context Separation:** Examples, suggested techniques, historical review
  comments, and implementation-agent claims are segregated into an explicitly non-authoritative
  context section. They illustrate ideas but cannot create unstated obligations or replace
  explicit requirements.
- **No Scope Enlargement or Symptom Masking:** Neither the controller nor the model may enlarge
  the correction to unrelated subsystem redesigns or silently replace it with a weaker,
  symptom-only workaround.

## Stable Bundle Identity & Explicit Supersession

Every repair handoff is bound to a deterministic, collision-resistant bundle identity
(`bundle_id`, e.g. `bnd_<hex>`):
- **Immutable Binding:** The bundle identity hashes the target repository, PR number,
  head/base branches, reviewed head commit, requirement-manifest revision, sorted canonical
  blocker IDs and accepted scopes, outcomes, and unmet status.
- **Durable Retention:** Bundles are durably recorded in the ledger database, enabling
  subsequent comparison and historical inspection.
- **Explicit Supersession:** When authority or scope changes (such as head SHA advancement,
  manifest revision changes, or accepted contract rebinding), the existing bundle is
  superseded by a new bundle that explicitly records `supersedes_bundle_id`.
- **Fail-Closed Staleness Fencing:** Senders attempting to deliver a stale or superseded
  bundle receive an explicit refusal/deferral. The system never silently mutates a bundle
  under an existing ID or falls back to an unvalidated raw review report.

## Unmet Correction Communication & Defect Warning

When a corrective generation fails to resolve an open blocker:
- **Unmet Concerns Identified:** The next handoff explicitly communicates which constituent
  concern IDs remain unmet.
- **Observable Failure Rationale:** Explains why the submitted changes failed to establish
  the required observable outcome.
- **Defect Warning:** Rejects claims that a commit, pass body, renamed test, green helper
  test, source-text assertion, or documentation assertion proves completion.
- **Identity & Oracle Preservation:** Retains the exact same blocker identity, scope, and
  production-boundary oracle rather than inventing a new obligation or shifting goalposts.

## Incremental Rereview & Coverage Carry-Forward

- **Initial Full Assessment:** Initial validation assesses every explicit requirement in the
  manifest against all changed files.
- **Focused Incremental Revalidation:** Incremental rereview revalidates open blockers and
  requirements whose behavior or prior evidence could be affected by the corrective diff,
  including shared state, persistence, synchronization, and production-boundary effects.
- **Evidence-Based Carry-Forward:** Unaffected prior coverage is carried forward only when
  accompanied by recorded unchanged-path evidence. An unverified requirement or invalid prior
  proof is never marked `VERIFIED` merely because no new change touched its file.
- **No Broad Re-exploration:** Rereviews do not restart broad exploratory testing. New
  rereview blockers require a demonstrated material explicit-requirement violation or a
  justified bounded test-oracle gap.
- **Real Regressions Preserved:** Genuinely demonstrated regressions caused by corrective
  changes are reported immediately; bounded rereview never hides real defects to force
  convergence.

## Strict Stopping Conditions & Merging Gates

Repair attempts stop only when:
1. Every applicable canonical blocker is independently resolved (`VERIFIED_CORRECTION`) or
   invalidated (`AUTHORIZED_INVALIDATION`).
2. All affected requirements are revalidated on the current head.
3. Unaffected coverage is validly carried forward with unchanged-path evidence.
4. No material specification or evidence gap remains.

Conversely, an omitted unresolved blocker, missing evidence, or retry exhaustion is never
converted into `PASS` or permission to merge.

## Universal Bounded Semantics

The bounded bundle contract governs all repair and rereview pathways:
- Local test-fix and repair prompts
- Codex Cloud, Jules, and Claude task follow-ups
- Ordinary review-repair routing of canonical findings
- Cached report replay and audit inspection
- Incremental reviewer prompt assembly

When bundle data is absent or stale, actionable delivery is deferred rather than falling
back to raw comment strings. Unrelated initial provider-instruction slots remain unchanged.


# Cross-Head PR Finding Reconciliation

Cross-head PR finding reconciliation provides deterministic deduplication and
thread continuity for pull request review findings before emitting GitHub review
publications (GitHub Issue #2137, Stage S3 of the convergent PR review tracking
family #2134). It prevents duplicate review root comments across commits, moved line
anchors, rephrased findings, and model substitutions, while preserving genuinely
distinct defects.

## Controller-Owned Blocker Reconciliation

Before publishing any review comment or submitting a native GitHub review, the
reviewer controller reconciles every incoming observation candidate (adversarial
finding, test oracle gap, or model critique) against the PR-scoped Canonical PR
Blocker Ledger:
- **Identity & Scope Matching:** Matches candidates by controller-owned blocker
  identity (`blocker_id`) or an invariant observation scope (authoritative production
  boundary, qualified requirement IDs, category, and described defect/invariant),
  rather than ephemeral GitHub comment IDs or exact line numbers.
- **Cross-Head Continuity:** Associates repeated findings across commit heads,
  refactored line numbers, paraphrased messages, and differing model backends with
  the existing canonical blocker.
- **Distinct Defect Preservation:** Observation candidates describing genuinely
  distinct defects (differing authoritative boundaries, distinct requirements, or
  differing failure invariants) reconcile to distinct blockers.
- **Non-Authorizing Ambiguity:** When reconciliation encounters ambiguous
  association across multiple active blockers or candidate roots, the ambiguity is
  treated as non-authorizing: it blocks speculative publications, resolves no
  blockers, and admits no spurious roots.

## Historical Review Root Bootstrapping

When bootstrapping from existing GitHub review threads on a PR:
- **Multipage Parsing & Authentication:** Parses review comments across all pages,
  filtering for authenticated bot identities (`[bot]` user login or known GitHub
  App identity). Replies (`in_reply_to_id is not None`) are excluded from root
  candidates.
- **Canonical Target Root:** The earliest authenticated numeric comment ID for a
  defect is preserved as the canonical target thread root. Subsequent duplicate roots
  are recorded as blocker aliases rather than emitting additional roots.
- **Compound Root Decomposition:** Retains all independently actionable corrections
  from compound historical review roots (e.g. comments containing both an
  implementation fix and a test oracle gap) rather than discarding extra obligations.
- **Unverified Author Comments Ignored:** Comments by non-bot or unverified authors
  (such as human contributor comments claiming "fixed") grant no authority, create
  no aliases, and do not resolve active blockers.

## Justified Category Transitions

The reconciliation lifecycle supports justified category transitions (such as
reclassifying an implementation defect to a contract or test oracle gap when newly
presented test evidence demonstrates the contract itself was incomplete):
- The transition records the justification reason in the ledger.
- Thread continuity is preserved by retaining the existing blocker ID and canonical
  root comment reference.

## Durable Publication Intent and Concurrency Fencing

To ensure safe, idempotent publication across network disruptions and racing processes:
- **Durable Publication Intent:** Before issuing any root-creating GitHub review
  request (single comment or batched submission), the publisher records durable
  publication intent in the ledger and acquires exclusive publication authority
  tied to the target PR and head revision.
- **Indeterminate Outcomes:** If a root-creating review request encounters an
  indeterminate outcome (network timeout, lost response, 5xx server error), the
  ledger intent remains pending until subsequent reconciliation verifies whether
  the GitHub comment exists before retrying or admitting another publication.
- **Definitive Rejection Handling:** If a publication request is definitively
  rejected (4xx client error, e.g. line outside diff), the pending intent is
  cleared and reported without stranding ledger lock state, allowing retries.
- **Head and CAS Fencing:** When multiple processes or racing workers evaluate
  the same PR, publication authority enforces compare-and-swap (CAS) tokens tied to
  the latest PR head SHA and ledger revision, rejecting stale publications.

## Publication Routing and Heuristic Isolation

- **Unified Publication Path:** Single-comment submissions, batched reviews
  (`POST /pulls/{pull_number}/reviews`), and replies route through the reconciliation
  pipeline. Every published root comment corresponds to an unrooted blocker, and
  every reply targets its canonical thread root.
- **Advisory Semantic Heuristics:** Advisory semantic similarity heuristics may
  propose candidate associations to the controller, but cannot bypass ledger
  invariant scope checks or mutate ledger state outside the deterministic
  reconciliation lifecycle.


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
  Sharing a requirement, diff-anchor file, or a few domain words does not establish
  equivalence: both the incorrect behavior and corrective outcome must have
  substantial shared content. Missing scope text or an empty authoritative boundary
  does not authorize association.
  The conservative lexical comparison ignores connective words and requires at
  least two shared content tokens covering half of each description, or identical
  nonempty token sets, separately for behavior and outcome. It is an advisory
  heuristic, not proof of semantic equivalence.
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

## Authenticated Root Reassociation

Historical roots authored by the configured reviewer App may recover a missing
`github_root_comment` alias by declaring an already-retained blocker with a
standalone `Blocker identity:` line. The declaration is interpreted only inside
the normalized API-origin, repository, and pull-request ledger namespace. Quoted
or fenced examples are ignored, while unknown IDs, contradictory declarations,
and conflicts with a retained root binding stop reconciliation without admitting
a substitute blocker.

Once associated, the root reuses the blocker's immutable accepted scope and
concern identities even if paths, prose, commits, or observation order change.
Repeated imports are idempotent. Compound roots may still own several genuinely
independent corrections; repeated representations of an owner do not create a
new owner or replace another correction's scope.

## Review-root publication confirmation and recovery

Ordinary reviewer-App publications retain their exact native-review request before
crossing the GitHub creation boundary. A successful review acknowledgement is only
an acceptance receipt: completion additionally requires paginated, review-specific
comment retrieval and an authenticated, non-reply root declaring each intended
canonical blocker identity. Review IDs are never used as comment aliases, and a
root-producing operation cannot be confirmed with an empty or partial alias set.
Distinct defects receive separate identities and roots even within one review.
When equivalent observations in the same batch reuse a blocker, their complete
finding sections and evidence are combined under one root at the first anchor;
the native review summary reports the actual attached thread count. This prevents
multiple roots declaring one blocker from stranding publication confirmation.
This change does not rewrite already-pending requests or historical GitHub roots.

If acknowledgement or root discovery is interrupted, the durable operation remains
pending and suppresses another publication for the same blockers. A later ordinary
publication entry reconciles the accepted review from the retained request, records
its receipt, and completes the original root associations without rerunning semantic
review. Ambiguous, missing, conflicting, or unauthenticated evidence stays explicitly
incomplete; genuinely root-free reviews retain their existing behavior.

GitHub's review-specific comment listing can omit `line`/`side` anchors or return
them as null while the individual comment endpoint retains the submitted anchor.
Recovery
fetches that individual receipt when the listing lacks these anchors, verifies
its comment ID, review ID, body, path, and reply relationship against the listing,
and then applies the existing author, blocker, and exact request-payload checks.
Missing or inconsistent receipts remain pending. Recovery uses GET requests and
never creates a replacement review or rewrites the retained request.

After a later commit relocates a comment, recovery compares `original_line` and
`original_start_line` from the individual receipt against the retained submission,
bound by `original_commit_id` to the validated head. Current line coordinates are
not evidence of the original publication position. A contradictory original
commit, missing required original coordinate, changed body/path, or mismatched
side still leaves publication pending. Recovery runs before a fresh ordinary
validation, including when the current head has no saved verdict, so an incomplete
older publication cannot cause repeated model runs whose publication is blocked
by that same operation.

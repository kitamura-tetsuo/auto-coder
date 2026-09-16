# Dedicated adversarial reviewer GitHub App

Adversarial validation verdicts are published as native pull-request reviews using
the separately configured `auto-coder-reviewer` GitHub App. Configure its `app_id`
and optional `client_id` under `[github-app-auto-coder-reviewer]` in
`~/.auto-coder/config.toml`; keep the private key at
`~/.auto-coder/auto-coder-reviewer.pem`. `PASS` maps to `APPROVE`, `NEEDS_FIX`
and `NEEDS_TESTS` map to `REQUEST_CHANGES`, and other outcomes map
deterministically to `COMMENT`.
Publication is pinned to the validated head SHA and fails closed without falling
back to the normal user credential.

For a `NEEDS_FIX` verdict, every actionable finding is submitted atomically as
its own independently resolvable review thread. Findings name a changed-file
anchor and include their complete requirement, counterexample, evidence, test
gap, and regression scenario. Valid diff line or range anchors are preserved;
missing or invalid line coordinates safely become file-level comments rather
than fabricated line locations. An invalid changed-file anchor or any review API
failure leaves publication unsuccessful. GitHub's existing review-thread
resolution state remains the only acknowledgement state used by merge gating.

This native review is the sole and authoritative publication path: Auto-Coder
does not additionally post the validation result as a regular user-authenticated
PR comment. The review body carries the same versioned machine-readable marker
(validator format/version plus the exact validated head SHA) used elsewhere, so
persisted validation state for a PR head can be recovered from the review after
a processing-loop restart. A review only counts as authoritative when it was
authored by the reviewer App's own resolved bot identity (fetched via `GET /app`
using the App's own credentials, never a configurable string) and carries the
marker for the exact head SHA in question; a lookalike review or comment from
anyone else is ignored. Reading this persisted state (`GitHubClient.get_pr_reviews_strict`
plus identity resolution) fails closed on any API or configuration error rather
than treating a lookup failure as "no prior result". Legacy validation comments
created before this change remain readable as a same-SHA fallback when no native
review exists yet for that head, but the comment-publication path itself is
never used for new results.


## Issue Review Boundary and Token Capabilities

The reviewer App also provides a dedicated publication boundary for Issue review comments (`publish_issue_review`).
To preserve the principle of least privilege, the client explicitly requests separate scoped installation tokens:
- Pull Request verdicts request tokens restricted to `pull_requests: write`.
- Issue findings request tokens restricted to `issues: write`.

This strict capability separation ensures that operations crossing contexts do not inadvertently leak permission scopes.
Like PR publication, Issue comment publication operates securely with zero fallbacks. If the App is unconfigured, or an identity/token lookup fails, the publisher does not fall back to the ordinary user credential or global/environment variables (e.g., `$GITHUB_TOKEN`).

### Issue specification/decomposition validation findings routing

`SpecificationValidationLifecycle.apply_blocked`/`apply_inherited_blocked` and
`DecompositionValidationLifecycle.apply_blocked` publish their BLOCKED
findings comments exclusively through `GitHubClient.publish_issue_review_comment`
(a thin wrapper over `publish_issue_review`), never through the ordinary
`add_comment_to_issue` credential path. A pre-existing comment only counts as
already-published when its body is an exact match for the decision's marker
**and** its author is the reviewer App's own resolved identity
(`GitHubClient.reviewer_app_identity`) -- a body-substring match alone is
never sufficient, and a marker-bearing comment from another actor is reported
as an unconfirmed conflict rather than silently accepted or reposted.

Label withdrawal (`implementation-ready` removal) always stays on the
ordinary controller credential: only the findings comment itself is
App-authored.

Each `ValidationDecision`/`DecompositionDecision` carries a
`publication_schema_version` and an optional `publication_receipt`
(confirmed comment id, publisher login, publisher App id). A decision
persisted before this routing existed keeps `publication_schema_version == 0`
forever and is never reinterpreted as App-authored, reposted, or edited. A
decision produced after this change is stamped `publication_schema_version =
1` before its first send attempt; `findings_published` is only trusted
without re-verification once a receipt is durably attached, so a crash
between an accepted POST and receipt persistence cannot masquerade as
complete -- it is re-checked against the live comment list on the next pass.

Both the specification decomposition-child (inherited) and decomposition
(parent-set) publication routes participate in the durable pending-work
store (`github_pending_work.py`), matching the standalone specification
route: a `GitHubRequestError` while posting the comment or removing the
label is deferred as a named effect (`diagnostic`/`readiness-withdrawal`)
and resumed by a registered stage handler
(`VALIDATION_PUBLICATION_STAGE` for specification,
`DECOMPOSITION_PUBLICATION_STAGE` for decomposition) rather than only being
recorded in a returned failure string.

A confirmed reviewer-App findings comment delivered back as its own
`issue_comment` webhook is recognized (marker + resolved reviewer identity)
and does not enqueue a redundant re-evaluation; this check is scoped to that
exact combination and is not a general bot/comment filter, so an unrelated
comment, human reply, or genuine Issue edit is unaffected.

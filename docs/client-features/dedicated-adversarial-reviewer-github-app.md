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

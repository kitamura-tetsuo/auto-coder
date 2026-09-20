---
name: Production two-tier PR merge gate
description: Durable final authorization for optional independent strong PR audits.
---

# Production two-tier PR merge gate

When `[backend_strong_pr_adversarial_validation]` is configured for a PR that is
eligible for ordinary adversarial validation, an ordinary PASS records convergence
but does not authorize merge. The independent `STRONG_AUDIT` must run through that
exclusive route and its authenticated publication must be acknowledged first.
Publication selects findings by the accepted round's producing claim ID, and
rejects an incomplete bundle before publishing. The separately assigned accepted
round ID must not cause retained findings to disappear from the published payload.
Strong findings are published as separate native review threads, one root comment
per finding, using the ordinary review publisher's diff-line anchoring helper.
Cited changed files and lines are preferred; portable findings without a usable
location retain their full evidence at an available changed-file diff anchor.
Each thread displays Requirement references, status, affected boundary, scenario,
expected/actual behavior, evidence, impact, and the regression scenario. The main
review summarizes the thread count and retains the exact JSON payload. Closure
reviews display disposition evidence without recreating strong finding threads.
Publication uses the versioned `github-reviewer-app:threads-v1` effect destination;
an older summary-only receipt does not confirm thread publication. Reconciliation
checks all expected comment bodies on the authenticated exact-head review.

Strong findings remain durable and are verified by `ORDINARY_CLOSURE` using the
ordinary PR route and the cumulative diff. A bounded closure can authorize the
repaired head without another strong call; expanded scope, changed contract/base or
strong policy, or an unrelated head requires a new audit. Unavailable or exhausted
strong routes remain pending (exhaustion uses the reported reset or a 1,800-second
cooldown), and restarts resume retained publication, delivery, or verification work.
The daemon retains a durable PR invalidation until that pending phase is runnable:
authoritative quota deadlines are preserved exactly, while contention and pending
effect delivery receive a bounded retry wake. Thus a due same-head cycle resumes in
the live controller without requiring another GitHub event, commit, or restart.

The final merge boundary compares the open PR's current head, reviewed base,
complete Requirements snapshot, strong-policy identity, outstanding findings, and
active attempts with durable completion. Diagnostics report the phase, selected
backend, audited/current heads, wait reason, finding IDs, and whether completion was
direct strong PASS or bounded ordinary closure. Closure establishes only that the
known contract and findings were verified; it does not prove that no unknown defect
exists.

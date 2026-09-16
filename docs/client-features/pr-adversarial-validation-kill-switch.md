# PR adversarial validation kill switch

Each adversarial-validation attempt binds repository-relative subprocesses to
its own detached worktree without changing the service process working
directory. The worktree HEAD is verified against the selected PR SHA before
evidence collection and at LLM and dynamic-check boundaries; unverifiable or
redirected execution fails closed. Concurrent attempts own and clean up only
their respective worktrees.

When `pr_adversarial_validation` is set to `false`, Auto-Coder completely bypasses
PR adversarial validation during PR candidate prioritization and merge-gate evaluation:
- No adversarial validation reviewer is invoked, scheduled, or executed for PRs.
- No new adversarial reviews, status comments, review threads, or attempt records
  are created, and attempt execution sequences are not incremented.
- Existing adversarial verdicts (such as `NEEDS_FIX` or `NEEDS_TESTS`) do not lower
  PR candidate priority, block internal merge eligibility, or trigger adversarial
  repair/re-review loops.
- Unresolved review threads authoritatively identified as originating from Auto-Coder's
  adversarial validator do not block internal merge eligibility through the generic
  review-thread gate.
- Existing adversarial review threads and published review verdicts remain unmodified,
  unresolved, and untouched on GitHub (they are not resolved, dismissed, deleted, or edited).
- Ordinary non-adversarial review threads (such as human reviewer comments) remain
  governed independently by `pr_review_thread_gate` and continue to block PR merge.
- Other independent PR gates (CI checks, GitHub branch protection, mergeability checks,
  auto-merge configurations, and mergeability remediation) remain strictly enforced.
- When `pr_adversarial_validation` is re-enabled, normal adversarial validation semantics
  resume for the exact current PR HEAD commit SHA; authoritative same-HEAD adversarial
  verdicts become active again, while verdicts for older HEADs do not become current.

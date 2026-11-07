# PR adversarial validation kill switch

Each adversarial-validation attempt binds repository-relative subprocesses to
its own detached worktree without changing the service process working
directory. The worktree HEAD is verified against the selected PR SHA before
evidence collection and at LLM and dynamic-check boundaries; unverifiable or
redirected execution fails closed. Concurrent attempts own and clean up only
their respective worktrees.

Linked-worktree fetch, registration and removal use the same cross-thread and
cross-process lease keyed by the physical Git common-directory identity. Linked
callers and path aliases therefore coordinate on one registry. The caller's
common-directory identity is rechecked after acquisition; a redirected caller
fails before any registry command. Git can expose an
initializing entry while its `commondir` file is still empty; the lease prevents
another cooperating fetch/add/remove from enumerating that incomplete entry. The
exact-head check runs before publication, and the lease is released before
validation/LLM work so independent attempts can overlap. Fetch also enumerates
worktree metadata and remains inside the preparation lease. It does not write
shared FETCH_HEAD or trigger background Git maintenance outside that lease. Failed cleanup
retains a registered/nonempty owned path for inspection rather than deleting it
behind Git's registry; a failed creation's unregistered empty directory can be
removed. No global prune or peer checkout removal is performed. This lease
coordinates participating Auto-Coder transactions; unrelated manual Git commands
that bypass it are outside that coordination boundary.

The concurrency regression preserves the overlapping-context and ambient-CWD
oracles. Peer exceptions abort synchronization and release waiters while keeping
the original failure visible. Its temporary directories and registry assertions
are scoped to that test run. Controlled partial-registration tests use actual
Git against an empty `commondir`, then prove add/add and remove/add serialization
without serializing the validation bodies. This changes private Git coordination
only; dashboard event schemas and review/admission decisions are unaffected.

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

# Monitored Checkout Ownership

  producer_checkout_preservation:
    description: "Keeps recurring producer maintenance from mutating the monitored repository checkout."
    implementation: |
      _producer_loop in src/auto_coder/automation_engine.py,
      BranchManager in src/auto_coder/branch_manager.py,
      switch_to_branch in src/auto_coder/git_branch.py
    behavior:
      - "Timer-driven and PR-woken producer maintenance performs update and Jules-session housekeeping without pulling, resetting, checking out, cleaning, merging, rebasing, cherry-picking, or otherwise repairing the monitored checkout."
      - "An idle daemon preserves branch or detached-HEAD identity, HEAD, index, tracked edits, untracked files, and an external in-progress Git operation across recurring maintenance intervals."
      - "Checkout synchronization remains inside explicit Issue and PR branch-processing boundaries: BranchManager uses switch_to_branch(..., pull_after_switch=True), and checkout, origin branch pull, or branch-verification failure makes branch entry fail."

## Concurrent Issue and PR checkout use

BranchManager holds a physical-checkout lease before reading the original branch
and until restoration finishes, including local implementation, result handoff,
commit, push and PR publication inside the branch context. PR checkout preparation
(including its reset/clean operations even with `perform_checkout=False`) and
local merge-conflict resolution use the same lease. A PR worker therefore cannot
invalidate an Issue invocation's captured caller checkpoint while its independent
clone is producing the implementation.

The lease uses the canonical absolute Git directory and its device/inode, so repository aliases and
subdirectories and bind-mount aliases coordinate, separate checkouts remain independent, and nested
operations on the same thread can reenter. It coordinates processes as well as
threads. Contended acquisition checks shutdown admission every 100 milliseconds;
unadmitted checkout work does not extend graceful shutdown. Entry failures and
branch restoration failures release the lease.
Contention and subsequent acquisition are logged once per wait, identifying the
physical checkout without claiming that a provider invocation has started.

This does not erase implementation-generation tombstones or indeterminate
dispatch receipts. An operator can authorize a new attempt of a previously
started generation through the existing `--force process-issues --only N --retry`
path; ordinary restarts continue to suppress duplicate implementation starts.

Regression coverage: `tests/test_checkout_lock.py`.

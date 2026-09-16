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

# Attempt Mechanism

  attempt_management:
    description: "Tracks retries per issue/PR and aligns work branches to the current attempt."
    tracking:
      - "Attempts are recorded as standardized comments 'Auto-Coder Attempt: <N>' with an optional detail suffix."
      - "get_current_attempt reads the latest attempt number from those comments (legacy timestamped comments stay compatible)."
      - "Comments are read with full pagination (100 per page), so attempt numbers stay correct on issues with more than one page of comments."
    deduplication:
      - "PR-driven increments carry a trigger marker 'trigger=pr-<number>-<head sha (12 chars)>' in the attempt comment."
      - "increment_attempt skips the increment when the latest attempt comment already records the same trigger, so a PR that keeps failing on the same commit bumps the counter only once."
      - "New commits on the PR produce a new trigger and therefore a new attempt; sub-issue propagation inherits the parent trigger."
      - "Callers that pass no trigger (e.g. Jules timeout handling, which runs once per closed PR) keep incrementing unconditionally."
    branching:
      - "Work branches follow issue-<number> for the first pass and issue-<number>/attempt-<N> for subsequent passes; parent branches cascade to sub-issues."
      - "When the local branch is behind the recorded attempt, Auto-Coder switches or recreates the correct attempt branch before continuing work."
      - "New attempt branches are created from the validated base branch (main or the parent's current attempt branch) to re-implement changes safely."
    fallback:
      - "PR failures that cannot be auto-merged (LLM CANNOT_FIX/unclear output, commit/push errors, failed merges or conflict resolution) trigger attempt increments for every linked issue."
      - "Jules PRs that still have failing CI more than [jules].pr_ci_timeout_hours (default 12) after PR creation are closed, and the attempt counter of the linked issue is incremented so the issue is retried from scratch."
      - "Jules sessions that do not open a PR within [jules].issue_pr_timeout_hours (default 12) are stopped, the attempt counter of the issue is incremented, and the issue is implemented by the backend_with_high_score backend instead."
      - "Conflict resolver fallbacks do the same when LLM-based conflict handling leaves unresolved markers or cannot push a clean merge, deduplicated by the PR head commit."
      - "When attempt count reaches 3 for any linked issue, the system automatically switches to the configured fallback backend (see [backend_with_high_score] configuration)."
      - "Backend fallback provides a fresh perspective using a different LLM after multiple failed attempts, improving chances of successful PR resolution."
    propagation:
      - "increment_attempt propagates attempt counter increments to all sub-issues to keep parent/child attempts in sync without reopening closed sub-issues."
    parent_handling:
      - "When processing a child issue whose parent issue is closed, the system automatically reopens the parent issue before continuing"
      - "This ensures branch selection, base branch selection, and attempt tracking use the parent issue context"
      - "Reopening closed parents maintains proper workflow continuity and prevents branch naming and base selection issues"
      - "When processing a child issue whose parent issue is closed, the system does not reopen the parent issue"
      - "Sub-issues are merged directly into the main branch upon completion"
      - "When all sub-issues of a parent issue are closed, the parent issue is processed via backend_with_high_score_cloud on a work branch from main"
      - "The prompt instructs the AI model to verify that main satisfies what the group of sub-issues was intended to implement, and implement any remaining requirements specified in the parent issue"

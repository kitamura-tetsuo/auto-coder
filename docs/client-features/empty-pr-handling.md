# Empty PR Handling

  empty_pr_handling:
    description: "Detection and requeueing of pull requests with zero effective diff against the base branch."
    zero_diff_detection:
      - "When a PR has zero changed files (or additions and deletions are zero / diff is empty), it is treated as a failed processing attempt even if the branch contains commits."
      - "The empty PR is closed automatically with an explanatory comment."
      - "The associated source issue is resolved from the PR body, session ID, branch name, or title."
      - "If the source issue was closed as a side effect of the PR relationship, it is reopened before reprocessing."
      - "The source issue's attempt counter is incremented, and its @auto-coder label is removed."
      - "The issue is requeued through the normal issue-processing path so the next attempt-based routing/fallback model takes over."
      - "Non-empty PR behavior remains unchanged."
    cloud_run_aware_handling:
      - "Before incrementing the attempt or releasing the @auto-coder label, _close_empty_pr() (src/auto_coder/pr_processor.py) checks whether the source issue's current attempt owns a durable CloudRun (CloudRunRepository.get(issue_number, attempt)) and, if so, asks cloud_run_policies.get_policy_for_provider(run.provider) for the run's CloudRunPolicy."
      - "If a policy is found and it denies the transition (CloudRunEvent(reason='empty_pr')), closing the PR stays a PR-local action: the Issue attempt is not incremented, the @auto-coder label is not released, and the issue is not requeued. The closed PR's number is still recorded on the CloudRun (CloudRunRepository.add_pull_request) without disturbing any other PR already associated with that run."
      - "For Codex Cloud (provider='codex-cloud'), CodexCloudRunPolicy is manual-only, so an empty PR alone never authorizes a new attempt or a replacement Codex Cloud task, even when it is the only known PR for that run. This lets another valid PR from the same multi-PR CloudRun continue through ordinary PR processing unaffected."
      - "When the issue's current attempt has no persisted CloudRun (e.g. Jules) or the provider has no registered policy, behavior is unchanged from the pre-#1607 attempt-increment/requeue flow described above."

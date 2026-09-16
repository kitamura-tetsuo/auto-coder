# Git Pull Recovery

  git_pull_recovery:
    description: "git_pull recovers automatically from local repository state that blocks a pull; uncommitted local changes are expendable."
    implementation: "git_pull, discard_local_changes, abort_in_progress_git_operations and reset_branch_to_remote in src/auto_coder/git_branch.py"
    behavior:
      - "Pull is attempted up to max_attempts times (default 3); each failure is classified from the combined stderr/stdout before a targeted remediation is applied."
      - "Local changes blocking the merge ('Your local changes ... would be overwritten by merge', 'Please commit your changes or stash them', 'untracked working tree files would be overwritten', 'you have unstaged changes') are discarded with 'git reset --hard HEAD' + 'git clean -fd', then the pull is retried."
      - "'git clean' never uses -x, so ignored files (build artifacts, virtualenvs, local configuration) are preserved."
      - "Unfinished merge/rebase/cherry-pick state and a stale .git/index.lock are cleared by aborting all three operations and deleting the lock file, then the pull is retried."
      - "Transient remote failures (host resolution, timeouts, 'RPC failed', 'early EOF', 'unable to access') are retried as-is with an exponential backoff capped at 5 seconds; they never trigger a hard reset."
      - "Diverging branches and merge conflicts are delegated to resolve_pull_conflicts as before."
      - "'no tracking information' / 'no such ref was fetched' is still reported as success because it is the normal case for a new branch."
      - "As a last resort the branch is hard-reset onto the remote branch (fetch + 'git reset --hard FETCH_HEAD' + 'git clean -fd')."
      - "The hard reset is refused when the branch has unpushed commits, so committed work is never lost; only uncommitted changes are discarded."
      - "discard_local=False disables every destructive step: the original pull error is returned instead."

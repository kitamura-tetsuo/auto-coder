# Jules PR Handling

  jules_pr_handling:
    description: "Rules applied to PRs created by Jules (google-labs-jules)."
    no_automatic_local_fixes:
      - "Auto-Coder never pushes automatic fix commits to a Jules PR branch: Jules does not recognize commits made to its branch by other actors, so such commits desynchronize the Jules session."
      - "CI failures are reported back to the Jules session instead, and Jules is expected to push the fix itself."
      - "Only an explicit local run that is already checked out on the PR branch keeps fixing the working copy directly."
    ci_timeout:
      - "When a Jules PR still has failing (completed) CI more than [jules].pr_ci_timeout_hours after it was created, the PR is closed as unfixable."
      - "The attempt counter of the issue linked to the PR is incremented so the issue is retried from scratch."
      - "The issue is resolved from the PR body links first, then from the Jules session ID, branch name, or PR title."
      - "Default timeout: 12 hours. Configurable via [jules].pr_ci_timeout_hours in config.toml."
      - "PRs whose CI is still running, or whose CI passed, are never closed by this rule."
      - "The check runs before every '@auto-coder label present' skip (candidate collection, single-target processing, and process_pull_request); a stale PR normally still carries the label from an earlier run and would otherwise never be revisited."
      - "Closing also releases the issue: the @auto-coder label that the dead Jules session kept on the linked issue is removed, and the issue is processed again in the same run (queued as a candidate in the daemon, processed inline for single-target runs)."
      - "PRs that are already closed are ignored, so the attempt counter is never incremented twice for the same PR."
    issue_pr_timeout:
      - "When a Jules session has been working on an issue for more than [jules].issue_pr_timeout_hours without opening a PR, the session is sent a 'stop' message and the issue is implemented by the backend_with_high_score backend instead."
      - "Default timeout: 12 hours. Configurable via [jules].issue_pr_timeout_hours in config.toml."
      - "The session-to-issue mapping comes from cloud.csv; sessions tracked against a PR number are ignored by this rule."
      - "Sessions whose outputs already contain a pullRequest, and issues that already have a linked PR on GitHub, are never stopped."
      - "Closed issues are skipped, and a session that fails to accept the stop message keeps the issue."
      - "The attempt counter of the issue is incremented, so the fallback starts from a fresh attempt branch instead of the one the Jules run left behind."
      - "The @auto-coder label the Jules run left on the issue is kept in place so no other instance picks the issue up while the fallback works on it; the fallback run passes check_labels=False so the label gate does not skip the issue it already owns."
      - "Stopped sessions are recorded in .auto-coder/jules_session_state.json so they are neither resumed nor handed over twice."
      - "The check runs during the hourly Jules full-session-list maintenance cycle, right after the Jules session resume/archive pass."
    session_listing_cache:
      - "Listing Jules sessions walks the whole paginated /sessions collection, which takes minutes once a repository accumulates thousands of sessions."
      - "The raw (unfiltered) listing is cached process-wide in jules_client, so every list_sessions call shares a single HTTP fetch regardless of the JulesClient instance or the repo_name filter used."
      - "Repository and ARCHIVED filtering still happens client-side on each call, so callers keep receiving their own filtered view."
      - "Each process waits approximately one hour after startup before its first full listing opportunity. The producer/run loop then starts a list-dependent Jules maintenance cycle at most once per hour, invalidating the cache immediately before the resume/archive, stale-issue, and recurrent-task passes."
      - "The hourly deadline advances before the listing is attempted, so a failed listing is not retried by a subsequent loop until approximately another hour has elapsed. Non-eligible loops skip all list-dependent maintenance rather than treating the cached snapshot as fresh."
      - "Direct operations on a known Jules session ID remain independent of the full-list maintenance schedule. The schedule is process-local and resets on restart."
    label_locking:
      - "Jules mode keeps the @auto-coder label on an issue for the lifetime of its session, so an aborted session would otherwise lock the issue permanently."
      - "With check_labels=False (--only and WIP-branch resume) an existing @auto-coder label no longer blocks processing; the label is left in place because the run does not own it."
    session_id_resolution_fallback:
      - "Session ID resolution from PR bodies extracts candidates across all supported patterns (explicit prefixes, URL parameters, provider session URLs such as Jules and Claude Routine, task IDs, and standalone IDs) in priority order."
      - "Candidates are checked against the local session tracking database (cloud.csv) first. If an earlier pattern (e.g. an ambiguous URL parameter like 'start_new_session=True') produces a candidate not recorded in cloud.csv, resolution returns to subsequent patterns (e.g. Claude Routine session URL Pattern 3a) in order rather than prematurely inferring an issue via comment search."
      - "Claude Routine session IDs are matched flexibly across 'session_' and 'cse_' prefix variants sharing the same identifier suffix."
      - "Comment search on GitHub is only attempted after all candidates fail local database lookup, and clearly invalid tokens (such as boolean literals or tokens under 4 characters) are excluded from comment search."
      - "GitHub Issue search for the comment-search fallback covers both the 'session_' and 'cse_' spelling of a Claude Routine session ID, not just the spelling extracted from the PR body: the two are lookup-equivalent, but GitHub's search index can surface the originating Issue under only one spelling even when it was created under the other. Each spelling gets its own independent first-5-results budget, so loose hits under one spelling never starve discovery under the other."
      - "Every candidate Issue returned by either spelling's search is deduplicated by Issue number, then re-read directly (not trusted from the search hit's title/snippet) and its body/comments are checked for a complete, word-boundary-delimited session-ID token before it can resolve the lookup; a Pull Request returned among the candidates is skipped."
      - "A search transport failure, or a failure reading a candidate Issue/its comments, leaves the fallback lookup unresolved (no issue is returned) rather than treating a partial observation as a confident empty result. Multiple distinct verified Issues make the lookup ambiguous and it also resolves to nothing rather than guessing; an ambiguous result is also never masked by falling back to a branch-name or PR-title guess."
      - "Pattern 4 (Jules Task URL, jules.google.com/task/<id>) accepts any nonempty sequence of ASCII letters, digits, underscores, or hyphens for <id>, matching Pattern 3's (Jules Session URL) character class rather than digits only."
    ci_wait_release_on_recovery:
      - "_should_skip_waiting_for_jules() (src/auto_coder/pr_processor.py) is the legacy 'waiting for Jules to fix CI failures' gate: normally it keeps deferring a Jules PR while a Jules session is present/non-completed, or while the last PR comment is Auto-Coder's CI-failure notice with no newer commit and the [jules].wait_timeout_hours window has not elapsed."
      - "That gate now accepts an already-obtained, current-head CI observation (github_checks) from its caller. When that observation is complete, KNOWN-availability, nonempty, aggregate-success, non-pending, and carries no observation error (RECOVERED_CI), the wait releases immediately: Jules session presence/state, PR-output association, comment history, and the wait timeout are never consulted in that case."
      - "A pending, failing, known-empty, partial, unavailable, throttled, or superseded observation (including a bare default GitHubActionsStatusResult with no backing snapshot) never establishes RECOVERED_CI; the gate falls straight through to its existing session/comment/timeout evaluation, so a still-failing head with a pending Jules CI-repair handoff keeps waiting as before."
      - "process_pull_request() obtains this CI observation once, before either of its two wait-gate calls (the second follows Jules session/PR-body linking), and reuses the same observation for its own later CI-eligibility evaluation rather than reading GitHub Actions twice for the same head."
      - "AutomationEngine._get_candidates() passes the same current-head observation it already computed for priority calculation, so the collector never excludes a recovered-CI PR that the processor would immediately let through."
      - "Recovery is decided fresh on each evaluation, not cached as a permanent per-PR/SHA exemption: a later run re-observes CI, and an older stale success cannot substitute for a newer pending/failing/unavailable reading of the same or a different head."
      - "Releasing this wait only lets ordinary PR evaluation proceed; it never marks or cancels the Jules session, resolves review findings, authorizes a merge, or sends another CI-repair request on its own."
    session_pr_author_resolution:
      - "Before deciding whether a PR is a Jules/Claude/session PR and before linking it to its source issue, the author login is resolved via the shared get_pr_author_login() helper rather than a direct 'user'/'login' dict-chain lookup."
      - "The resolved login is the first nonempty string among: a plain string 'author' field, 'author[\"login\"]' when 'author' is a dict, and 'user[\"login\"]' when 'user' is a dict, checked in that order."
      - "A missing 'user'/'author' field, a null value for either, or a present-but-empty/null 'login' inside either dict never raises; resolution simply falls through to the next candidate and yields no login (empty string) when none is usable."
      - "A PR body containing a Claude Code session URL or a Jules session/task URL still enters session-based issue resolution and, on a uniquely recorded local session-to-issue mapping, still gets linked even when the author login could not be resolved; an unresolvable author never causes the PR to be treated as not applicable."
      - "The three special-prefix Jules PR titles ('🛡️ Sentinel: ', '🎨 Palette: ', '⚡ Bolt: ') remain exempt from automatic session-issue body linking regardless of which author representation was supplied."


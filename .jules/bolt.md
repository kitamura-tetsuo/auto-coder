## 2024-05-23 - [N+1 GitHub API Calls in PR Processing]
**Learning:** Processing multiple PRs triggers individual API calls for each PR's CI status (`gh api .../check-runs`), causing significant latency and rate limit usage.
**Action:** Implemented `preload_github_actions_status` to batch-fetch workflow runs for all PRs in a single `gh run list` call, populating the cache upfront.

## 2026-02-17 - [Optimized tail_window with Suffix Buffer]
**Learning:** `splitlines()` on large strings (e.g., full log files) creates massive lists and copies data unnecessarily, causing O(N) performance issues.
**Action:** Implemented a suffix buffer strategy (checking last 4000 chars) to extract trailing lines in O(1) time relative to total string size.

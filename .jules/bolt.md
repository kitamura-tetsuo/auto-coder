## 2024-05-23 - [N+1 GitHub API Calls in PR Processing]
**Learning:** Processing multiple PRs triggers individual API calls for each PR's CI status (`gh api .../check-runs`), causing significant latency and rate limit usage.
**Action:** Implemented `preload_github_actions_status` to batch-fetch workflow runs for all PRs in a single `gh run list` call, populating the cache upfront.

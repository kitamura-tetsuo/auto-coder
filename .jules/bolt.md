## 2024-05-23 - [N+1 GitHub API Calls in PR Processing]
**Learning:** Processing multiple PRs triggers individual API calls for each PR's CI status (`gh api .../check-runs`), causing significant latency and rate limit usage.
**Action:** Implemented `preload_github_actions_status` to batch-fetch workflow runs for all PRs in a single `gh run list` call, populating the cache upfront.

## 2026-01-10 - [O(N) Memory in String Suffix Extraction]
**Learning:** `change_fraction` used `s.splitlines()` on the entire string just to get the last 20 lines. For large test outputs (50MB+), this caused massive memory allocation and latency (0.6s+ per call).
**Action:** Optimized `tail_window` to slice a small suffix buffer (4000 chars) before splitting lines, reducing complexity from O(N) to O(1) for large inputs.

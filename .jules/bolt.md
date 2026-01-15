## 2024-05-23 - [N+1 GitHub API Calls in PR Processing]
**Learning:** Processing multiple PRs triggers individual API calls for each PR's CI status (`gh api .../check-runs`), causing significant latency and rate limit usage.
**Action:** Implemented `preload_github_actions_status` to batch-fetch workflow runs for all PRs in a single `gh run list` call, populating the cache upfront.

## 2024-05-24 - [Expensive String Splitting in tail_window]
**Learning:** `change_fraction` utility used `splitlines()` on entire strings (often large test logs) just to check the last 20 lines. This caused O(N) memory and CPU usage where O(1) suffices.
**Action:** Use `s[-4000:].splitlines()` to process only the relevant tail of the string.

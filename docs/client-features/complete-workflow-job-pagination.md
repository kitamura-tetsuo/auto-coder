# Complete workflow job pagination

GitHub Actions job details and logs use `list_all_workflow_jobs` in
`util/gh_cache.py` to read every REST page through the cached API client.
Requests use 100 jobs per page and continue until the reported total is reached;
without a reported total, a short page ends the listing. A failed request or an
empty page before the reported total raises rather than returning partial jobs.

This applies to detailed CI checks, PR-filtered history, run-URL log lookup,
historical log matching, and merged action-log summaries. A failure after the
first 30 or 100 jobs therefore reaches the existing repair route with its job
ID and log URL instead of indefinitely reporting no specific failed checks.
Advisory workflow exclusions, latest-job deduplication, exact-head repair
admission, provider ownership, and repair allowance gates retain their behavior.

The change is observability-neutral: it completes the input of existing CI and
repair stages without adding an origin, gate, routing branch, outcome, durable
resumption path, or structured trace field. It does not represent collected job
details as a successful repair. Regression coverage is in
`tests/test_workflow_job_pagination.py`.

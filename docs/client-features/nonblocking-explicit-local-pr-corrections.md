# Nonblocking explicit-local PR corrections

Eligible `LOCAL_REQUIRED` PR review corrections are transferred to the durable
local-job store after the exact head, same-repository branch, canonical feedback
identities, bounded prompt, selected local backend, repair-store incarnation,
and repair-allowance generation are captured. The PR worker returns a deferred
`pending` result as soon as that transaction commits; it does not wait for the
model, workspace, commit, push, or validation.

The independent local runner re-reads uncached PR routing metadata and the
original repair and allowance ownership immediately before provider entry. A
closed PR, removed explicit-local declaration, changed head/ref/repository, or
changed allowance prevents entry without charging invocation. Accepted and
running jobs retain both the repair-store and local-job ownership, so replayed
PR processing cannot dispatch the same attempt again.

Execution continues through the existing detached exact-head correction path,
including invocation-time allowance charging, commit/push lease protection,
publication recovery, `CANNOT_FIX`, no-change, and awaiting-validation states.
Completion durably invalidates the PR for current-head processing; the runner
result itself never resolves findings or grants merge approval.

Regression coverage is in `tests/test_pr_correction_job.py` and the production
route assertions in `tests/test_local_review_repair.py`.

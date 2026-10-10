# Nonblocking explicit-local PR corrections

Eligible `LOCAL_REQUIRED` PR review corrections are transferred to the durable
local-job store after the exact head, same-repository branch, canonical feedback
identities, bounded prompt, selected local backend, repair-store incarnation,
and repair-allowance generation are captured. The PR worker returns a deferred
`pending` result as soon as that transaction commits; it does not wait for the
model, workspace, commit, push, or validation.

The independent local runner re-reads uncached PR routing metadata, actionable
review-thread identities, and the original repair and allowance ownership both
before preparation and again after detached-worktree preparation at the actual
model-entry boundary. A closed PR, changed route or cloud owner, addressed
feedback, changed head/ref/repository, or changed allowance prevents entry
without charging invocation. Backend construction occurs during pre-entry
authorization, while the second authority read fences changes that happen
during construction or workspace preparation. Confirmed unavailable backends
and definitely unstarted preparation failures return the same durable job for
retry without replacing its repair incarnation or allowance generation.
Accepted and running jobs retain both ownership identities, so replayed PR
processing cannot dispatch the same attempt again. Runner instances restrict
both invocation and downstream notification to registered job kinds,
preserving the separate Issue implementation owner.

Execution continues through the existing detached exact-head correction path,
including invocation-time allowance charging, commit/push lease protection,
publication recovery, `CANNOT_FIX`, no-change, and awaiting-validation states.
If the process stops after the controller checkpoints `publication_pending`
but before the runner writes its result artifact, restart recovery uses that
domain checkpoint to resume publication without another editing-model call.
Completion durably invalidates the PR for current-head processing; the runner
result itself never resolves findings or grants merge approval.

Regression coverage is in `tests/test_pr_correction_job.py` and the production
route assertions in `tests/test_local_review_repair.py`.

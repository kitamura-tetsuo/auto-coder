# Live Muse local-job continuation

Issue implementation and PR review-correction jobs keep their accepted local-job
identity, workspace, provider session, and Muse host while attributable background
tools finish. The foreground turn is not a completion boundary. On a runtime that
earns background support from correlated task inventory, result delivery, session
replay, and native-wake evidence, the same host follows the native continuation and
returns only assistant output that incorporates the completed result frontier.

The final output is recorded against the current durable frontier before host
shutdown and confirmed only after the invocation-owned writer process group settles.
The local runner supplies the same executor fence when recording its result, so an
intermediate message cannot make downstream Issue publication or PR validation
eligible. Unsupported or ambiguous background semantics, stale frontiers, missing
result delivery, timeout, cancellation, and unknown settlement remain failures or
reconciliation work. Ordinary non-Muse and single-turn Muse calls retain their
existing behavior.

PR corrections bind the detached exact-head worktree to the accepted local job
before entering Muse. This extends the same continuation evidence boundary to the
existing correction allowance and publication lifecycle without reserving another
allowance or creating a second implementation attempt.

Run `bash scripts/test.sh tests/test_muse_msp.py tests/test_local_job_runner.py
tests/test_pr_correction_job.py` for the executable transport, fencing, runner, and
both domain-adapter regressions.

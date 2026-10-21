# Startup recovery for local PR repairs

Daemon startup opens the same repository-scoped local-job authority used by
PR admission and registers CI-failure, merge-conflict, and review-correction
adapters on one bounded PR runner. Before ordinary Issue or PR workers start,
the runner performs recovery-only checkpoint reconciliation and the exact-head
publisher observes retained CI/conflict results. Provider polling starts only
after the startup reconciliation barrier.

Unfinished CI/conflict publication is represented by a stable pending-work
identity containing the local job and execution incarnation. Repeated runner
notifications and restart scans only ensure that identity exists: they do not
reset its due time, throttle attempts, operational block, or running claim.
The registered handler calls only the PR repair finalizer, so publication
recovery cannot invoke an editor or substitute a newer PR-head identity.

Entered invocations with uncertain ownership remain governed by
`LocalJobRunner.recover_unsettled`; dead or missing process evidence does not
authorize another model call. Foreign-repository rows are excluded by both
runner discovery and finalization rediscovery. The pending-work scheduler's
existing status snapshot exposes unfinished finalization, while finalizer
effect receipts remain authoritative for commit, push, invalidation wake, and
settlement.

Focused coverage for idempotent rediscovery and preserved backpressure lives
in `tests/test_github_pending_work.py`; runner checkpoint and exact publisher
coverage remains in `tests/test_local_job_runner.py` and
`tests/test_pr_repair_finalizer.py`.

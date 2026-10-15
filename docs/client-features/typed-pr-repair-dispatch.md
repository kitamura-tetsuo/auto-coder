# Typed PR repair dispatch

CI-failure and merge-conflict effects enter a typed request/result boundary
after the PR progress policy has selected a repair route. The request fixes the
repository, PR, current head, repair kind, and the already-established cloud
owner or local execution origin. The adapter does not select a provider and
cannot grant repair authority or introduce fallback behavior.

The result distinguishes refusal, deferral, accepted-or-uncertain delivery,
failure, and an applied result. Attempt, receipt, resulting-head, and
synchronization evidence are separate facts with an explicit unknown state;
action text is diagnostic only. Existing producers continue to own durable
claims, allowance charging, delivery reconciliation, local Git operations,
configured test execution, and replay behavior.

The production Codex Cloud CI continuation and cloud conflict paths use this
boundary while retaining their existing helpers and stores. A cloud refusal
does not become local work, an accepted or uncertain effect is not re-sent by
the adapter, and applying a repair does not provide CI, review, or merge
authority. Local repair remains synchronous and is not converted into a job.

Focused regressions are in `tests/test_pr_repair_dispatch.py`; broader
production-origin and durable replay coverage remains in
`tests/test_pr_lifecycle_contract.py`, `tests/test_codex_cloud_pr_ci_flow.py`,
and `tests/test_cloud_conflict_delegation.py`.

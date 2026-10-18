# Current responsibility map

Source snapshot: **`0769b84eaabcf2d0c6a80f6c62660bd179003e02`** (2026-10-18 UTC). [Scope and notation](README.md).

This map identifies current owners and consumers. It is not a proposal to make every row a new service, queue or database.

| Responsibility | Current production location | Decisive input/output | Coupling to preserve while extracting |
| --- | --- | --- | --- |
| Issue runtime startup and recovery composition | `issue_runtime_composition.py`, wired by `automation_engine.py` | Repository-scoped pending-work handlers, accepted retry rediscovery and local Issue runner | Startup barrier, one registration, retained due times and repository filtering |
| Implementation execution orchestration | `issue_implementation_service.py`, entered by `automation_engine.py` | Current approved G, exact ownership, retry authority, dispatch result and settlement | Stale approval must not start a provider; accepted local work transfers its execution |
| Ordinary provider adapter composition | `ordinary_issue_adapter.py`, wired by `issue_processor.py` | Ranked configured providers and durable adapter outcomes | Accepted or uncertain work stops fallback; only confirmed non-start advances it |
| Provider order, admission and fallback | `issue_dispatch.py` | Configured lane, retained owner, accepted/uncertain/non-start outcomes | Uncertainty and accepted work stop replacement starts |
| Durable local invocation identity | `local_job_handoff.py` | Job kind, input, incarnation, entry checkpoint and effect state | Submission fence and same-invocation ownership |
| Local process/job scheduling and recovery | `local_job_runner.py` | Claim, liveness and recorded result | Unknown liveness is not dead; post-entry recovery is not fresh inference |
| Local Issue execution | `issue_local_job.py` | Captured generation/source plus private workspace result | Controller and peer workspace correctness |
| Issue result publication | `issue_job_finalizer.py` | Exact artifact, continuation status and reconciled push/PR effects | No model rerun for publication recovery; associate only matching owner |
| Local PR review correction | `pr_correction_job.py` | PR head, route, reserved allowance, repair claim and result | Job settlement versus independent repair validation |
| CI observation conversion | `util/github_action.py` | Current-head evidence and in-progress/success classification | Shared consumers include candidate filtering and PR processing |
| PR progress, repair and merge decisions | `pr_processor.py` | CI, mergeability, origin, review and cleanup evidence | Several independently durable authorities meet in one large module |
| Two-tier review cycle | `pr_review_cycle.py`, `two_tier_pr_gate.py` | Claims, rounds, findings, publication and H/B/M/P completion | Reviewer result is not publication confirmation |

## Where responsibilities currently overlap

`IssueRuntimeComposition` owns assembly rather than domain state, and `IssueImplementationService` owns orchestration rather than provider policy. The engine is still not only a queue reader: it also reconciles lifecycle state and filters PR candidates using CI. The PR processor is not only a merge executor: it performs CI interpretation, provider continuation, local repair, review application and cleanup coordination. This overlap is visible in the empty-CI reachability problem, where the candidate collector can prevent the handler from choosing conflict remediation.

Local asynchronous execution has already been extracted for Issue implementation and PR review correction. Treating those components as absent would create an unnecessary rewrite. Equally, their existence does not mean the CI/conflict paths have adopted the same lifecycle.

Publication and ownership are also split. An accepted provider task, a saved model result, a pushed branch and an associated PR can become durable at different times. No inspected call chain establishes one transaction across all of them.

## Candidate extraction boundaries, not approved target design

A useful first boundary is a PR progress decision that consumes explicit observations and produces a next action without redefining their producers. Another is an adapter boundary between a repair request and provider/local execution, preserving existing receipt and recovery semantics. Cleanup/retirement should remain a separate responsibility rather than being hidden in a generic successful-result handler.

These are hypotheses to evaluate with the [test probes](refactoring-seams-and-tests.md), not new requirements for all providers. An event bus, database replacement, global state machine or security subsystem is not implied by this map.

Sources: [engine][engine], [implementation worker][worker], [Issue integration][issue], [dispatch][dispatch], [job state][jobs], [runner][runner], [local executor][local], [finalizer][finalizer], [correction][correction], [CI][ci], [PR processor][pr], [review cycle][cycle], [two-tier gate][gate].

[engine]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/automation_engine.py
[worker]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/issue_implementation_worker.py
[issue]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/issue_processor.py
[dispatch]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/issue_dispatch.py
[jobs]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/local_job_handoff.py
[runner]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/local_job_runner.py
[local]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/issue_local_job.py
[finalizer]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/issue_job_finalizer.py
[correction]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/pr_correction_job.py
[ci]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/util/github_action.py
[pr]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/pr_processor.py
[cycle]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/pr_review_cycle.py
[gate]: https://github.com/kitamura-tetsuo/auto-coder/blob/0769b84eaabcf2d0c6a80f6c62660bd179003e02/src/auto_coder/two_tier_pr_gate.py

# Pull-request CI supersession

The `PR Tests`, `Browser Tests`, `OpenCode Live Tests`, `Muse Live Tests`,
`Supervisor Integration Tests`, and `Test Source for Runtime` workflows ask
GitHub Actions to cancel an older active invocation when another invocation of
the same workflow arrives for the same pull request. Workflow identity and pull
request number form the cancellation boundary, so runs for different workflows
or pull requests remain independent.

Push and `workflow_dispatch` invocations use the event name and unique run ID
instead. They neither share a pull-request group nor cancel or serialize one
another under this policy. GitHub's concurrency scheduler is arrival-based: a
rerun remains attached to its original event and revision, but can supersede a
different active invocation in the same group. Cancellation is not successful
test evidence, and the retained run must execute its existing checks for its own
checked-out revision. No check result or tested-source identity is copied between
runs.

## Timing observations

Queue duration below is `started_at - created_at`; execution duration is
`completed_at - started_at`, using the Actions job timestamps. These observations
do not establish a runner-capacity cause or promise a fixed reduction.

Before this policy, PR Tests run
[`41565140889`](https://github.com/kitamura-tetsuo/auto-coder/actions/runs/41565140889)
reported the following completed jobs on 2026-11-03:

| Job (job ID) | Queue | Execution | Result |
| --- | ---: | ---: | --- |
| Lint & Type Check (`125684743248`) | 10m38s | 1m10s | success |
| Tests with Coverage 4/4 (`125684743367`) | 8m23s | 8m04s | success |
| Tests with Coverage 3/4 (`125684743409`) | 10m33s | 8m04s | success |
| Tests with Coverage 1/4 (`125684743410`) | 12m18s | 7m10s | success |
| Tests with Coverage 2/4 (`125684743463`) | 11m50s | 5m23s | success |
| Tests with Coverage aggregate (`125691376806`) | 10m25s | 3s | success |

For comparison, the different-revision run
[`41556719372`](https://github.com/kitamura-tetsuo/auto-coder/actions/runs/41556719372)
had 3–4 second shard/lint queues, a 7 second aggregate queue, and finished in
8m16s from job creation to final completion. It is not a controlled benchmark.

The post-change hosted overlap observation is recorded only after an older run is
seen pending or running before submission of its replacement. Until that sequence
is executed, cancellation status, retained revision, retained test result, and
post-change queue/execution durations are **unavailable**, rather than inferred
from the workflow definitions.

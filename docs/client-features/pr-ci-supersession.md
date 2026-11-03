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

For the post-change hosted overlap check, revision
`074d246cd1f2e0f6f6f5bc378139faf24c9166aa` was observed in the `queued` state
before its replacement was submitted. Its identified runs were PR Tests
[`41730325906`](https://github.com/kitamura-tetsuo/auto-coder/actions/runs/41730325906),
Browser Tests `41730325605`, OpenCode Live Tests `41730325708`, Muse Live Tests
`41730325818`, Supervisor Integration Tests `41730325761`, and Test Source for
Runtime `41730325637`. After revision
`140b43dae8451f7c056498264a16faa11f24cf00` was submitted, readback showed the
older PR Tests, Browser Tests, OpenCode Live Tests, Supervisor Integration Tests,
and Test Source for Runtime runs completed with `cancelled`; the older Muse run
had already completed successfully before supersession could apply.

The retained revision's Muse Live Tests run
[`41730433484`](https://github.com/kitamura-tetsuo/auto-coder/actions/runs/41730433484)
completed successfully, recording an actual test result for that exact revision
rather than treating the YAML inspection as hosted evidence. Its job
`126246006403` queued for 55s and executed for 26s. For a cancellation timing
observation, older OpenCode job `126245636627` had a 0s queue and ran for 44s
before its run concluded `cancelled`. Other retained jobs were incomplete at this
readback, so their result and post-change queue/execution durations remain
**unavailable**, rather than inferred from the workflow definitions.

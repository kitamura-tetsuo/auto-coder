# Restored PR lifecycle CI

The issue-2500 maintenance window is retired. Every pull request now enters the
ordinary lint/type job and all four coverage shards, with the aggregate job
failing when any shard does not succeed. There is no issue allowlist,
retained-only route, skip/xfail, relaxed threshold, or maintenance selector.
The browser, OpenCode-live, Muse-live, and version workflows remain independent
and retain their existing selections.

Every later push to `main` enters the normal Publish Beta tested-build job. Its
non-OpenCode-live test command precedes image construction and publication. The
Advance Beta workflow still reads the jobs belonging to its exact triggering
Publish Beta run and requires exactly one completed successful `Build tested
artifact` job. Consequently, a delayed completion from a historical paused run
cannot advance the channel even though the pause configuration and selector have
been removed.

## Integrated lifecycle boundary

The integration is based on the characterized boundary at
`6c0cc09cde557985ecc484dbc35cb90fd85486f0`, the phase-policy extraction at
`784cf08`, review composition at `e6f2655`, shared-policy wiring at `5e140d6`,
and typed CI/conflict dispatch at `9e5c2d8`. Candidate collection and the PR
handler use `pr_progress_policy`; CI and conflict effects use
`pr_repair_dispatch`; green-current-head processing enters the review
coordinator before the existing final merge authority. The existing durable
receipt, review-cycle, validation-attempt, implementation-slot, and merge
authorities remain owners of their state; no duplicate orchestration branch is
retained for compatibility.

`tests/test_pr_lifecycle_contract.py` is the joined production-origin regression
boundary. It controls external GitHub/provider/Git effects while exercising
actual candidate-to-handler routing, current and stale CI observations, cloud
repair and conflict identities, review/strong completion, durable replay,
newer-head and newer-attempt fencing, and the final positive merge effect.
`tests/test_ci_restoration.py` separately locks down the restored workflow and
the delayed-run publication guard. Local-LLM CI/conflict expansion remains out
of scope.

Legacy PR-flow tests no longer manufacture a passing aggregate result without
an observation. Their shared controlled adapter creates check-run evidence
through `github_ci_observer.observe_ci`, so phase fencing and final merge
authority remain part of the test oracle after the lifecycle extraction.

## Verification record and limitations

The bounded local verification commands and observed results are recorded in
the implementing pull request. The pull request's GitHub check suite is the
authority for current-head NORMAL CI run identifiers and the complete four-shard
result. A post-merge Publish Beta run cannot exist during pre-merge verification;
its eventual result is a separate main-push publication observation and is not
claimed here as a published artifact.

This restoration changes workflow admission but not Auto-Coder runtime origins,
outcomes, provider routing, durable resumption, trace schemas, or dashboard view
projection. Production trace emissions and the runnable inventory in
`docs/dashboard-observability.md` therefore require no change.

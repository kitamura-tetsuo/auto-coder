# Bounded PR test shards

Each PR Tests shard passes the explicit `--ci-tests-only` mode through its
bounded runner to the target-repository test script. This preserves the shard's
pytest selection while avoiding a duplicate quality-tool installation and
quality run; direct runner and script calls still use the complete gate by
default. The workflow's separate **Lint & Type Check** job runs repository
hygiene, Black, isort, Flake8, and package-wide mypy once, and the aggregate
**Tests with Coverage** check requires both that job and all four shards.

On 2026-11-04, a small production-entry measurement used `CI=true bash
scripts/test.sh
tests/test_pr_test_shard_workflow.py::test_production_step_passes_correct_shard_once`
and the same command with leading `--ci-tests-only`. Both passed the same four
parameterized wrapper cases. The complete path took 58 seconds (pytest: 2.05
seconds), and the explicit path took 11 seconds (pytest: 6.02 seconds). This is
only production-entry overhead evidence, not an ordinary-shard measurement.

The comparable ordinary-shard measurement used the implementation revision,
the prepared locked test environment, group 1, the generated normalized copy
of `tests/data/pr-test-durations.json`, and the production marker selection.
The commands differed only by the explicit mode:

```text
CI=true uv run python scripts/run_pr_test_shard.py --group 1 --attempt-timeout 480 --termination-grace 10 --max-attempts 2 --log-dir .agent-tmp/issue2729-before-valid-20261104
CI=true uv run python scripts/run_pr_test_shard.py --ci-tests-only --group 1 --attempt-timeout 480 --termination-grace 10 --max-attempts 2 --log-dir .agent-tmp/issue2729-after-valid-20261104
```

Both collected 10,042 nodes, deselected 5,701, and selected the same 4,341-node
group. The local command harness interrupted the complete-path observation
after approximately 300 seconds at 53% pytest progress and the explicit-path
observation after approximately 250 seconds at 38% progress, so neither result
is reported as a pass or a measured speedup. There was no Actions queue time.
The complete-path trace spent approximately 50 seconds in quality provisioning,
Black, isort, Flake8, and mypy before pytest started; the explicit trace entered
pytest without that stage. Actual pytest work remained incomplete and its
elapsed time is therefore not compared. CI owns the complete shard result.

Each shard has a 480-second attempt deadline. A timed-out process group is terminated (with a
10-second TERM grace before KILL) and retried exactly once; ordinary failures,
cancellation, launch errors, and cleanup errors are not retried. Attempt logs
are retained separately, retry reports start clean, and a 24-minute Actions
step limit provides an independent safety boundary.

Shard output includes individual test names and emits Python thread stacks when
a test phase exceeds 30 seconds, so line-buffered collection cannot disguise a
later stalled test as the last completed file. Codex MCP fallback regressions
exercise finite stdin and real subprocess EOF and assert reader-thread cleanup;
unbounded mock streams must not leak busy readers into subsequent tests.
This is test infrastructure only: production processing, structured trace events,
and dashboard projections are unchanged.

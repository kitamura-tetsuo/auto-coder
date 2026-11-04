# Bounded PR test shards

Each PR Tests shard passes the explicit `--ci-tests-only` mode through its
bounded runner to the target-repository test script. This preserves the shard's
pytest selection while avoiding a duplicate quality-tool installation and
quality run; direct runner and script calls still use the complete gate by
default. The workflow's separate **Lint & Type Check** job runs repository
hygiene, Black, isort, Flake8, and package-wide mypy once, and the aggregate
**Tests with Coverage** check requires both that job and all four shards.

On 2026-11-04, a local comparable invocation at the implementation revision
used `CI=true bash scripts/test.sh
tests/test_pr_test_shard_workflow.py::test_production_step_passes_correct_shard_once`
and the same command with leading `--ci-tests-only`. Both selected and passed
the same four parameterized pytest cases. The default path took 58 seconds
wall time (pytest reported 2.05 seconds); the explicit path took 11 seconds
(pytest reported 6.02 seconds). There was no Actions queue time in this local
measurement. The 47-second wall-time difference therefore records removal of
the repeated provisioning and quality stage for this run, not eliminated test
work or a portable performance guarantee.

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

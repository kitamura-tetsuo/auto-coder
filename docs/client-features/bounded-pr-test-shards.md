# Bounded PR test shards

Each PR Tests shard runs the complete target-repository test script with a
360-second attempt deadline. A timed-out process group is terminated (with a
10-second TERM grace before KILL) and retried exactly once; ordinary failures,
cancellation, launch errors, and cleanup errors are not retried. Attempt logs
are retained separately, retry reports start clean, and a 12-minute Actions
step limit provides an independent safety boundary.

Shard output includes individual test names and emits Python thread stacks when
a test phase exceeds 30 seconds, so line-buffered collection cannot disguise a
later stalled test as the last completed file. Codex MCP fallback regressions
exercise finite stdin and real subprocess EOF and assert reader-thread cleanup;
unbounded mock streams must not leak busy readers into subsequent tests.
This is test infrastructure only: production processing, structured trace events,
and dashboard projections are unchanged.

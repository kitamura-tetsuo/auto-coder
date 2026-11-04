# CI integration timing diagnostics

The Supervisor Integration Tests and each complete-source shard set
`AUTO_CODER_CI_TIMING_DIR`. Setting the same variable to an empty directory
enables diagnostics for a focused local pytest invocation; leaving it unset
keeps the observer disabled.

Each process creates an invocation-specific, append-only `timing-*.jsonl` file.
Records identify the outer checkout, GitHub workflow/run/attempt/job when those
values exist, and a process and invocation. Missing GitHub values on local
runs remain `null`; they are not inferred. Native `pytest_phase_report`
records contain the exact node ID, setup/call/teardown phase, pytest's own
duration, and outcome. Explicit harness spans are separate records and may
overlap or nest, so their durations must not be summed as wall time.

CI uploads the JSONL files and `timing-summary.json` (or a shard `summary.json`)
even after a failed test step. The summary separates native pytest phases from
nested harness spans and lists a start without an observed end under
`incomplete_spans`; incomplete never means zero duration or success. Use the
workflow, run, attempt, job, checkout, source revision, lock, runtime, target
role, and caller-provided comparison label to compare like-for-like records.
Malformed or unreadable records make the summary `incomplete` or `unavailable`;
they never change the underlying helper or test result.

These files are diagnostic evidence only. They do not make a test pass,
authorize READY or publication, or replace the test process exit status. A
diagnostic write or summary failure likewise cannot replace the original test
result. The reviewed PR shard weights in `tests/data/pr-test-durations.json`
remain independently maintained and are not updated by this facility.

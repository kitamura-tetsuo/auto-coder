# CI integration timing diagnostics

## Management CI retirement (2026-11-13)

The owner approved retiring the `management-self-update`, `fresh-host`,
`isolated-containers` and `operator-upgrade` CI jobs and their dedicated planning
and aggregate wiring. These exercise the discontinued proprietary management,
bootstrap and supervisor deployment architecture. The detailed native-profile
procedures below are historical, not active CI gates or acceptance evidence.

`Supervisor Integration Tests` retains the complete `worker-bridge` job unchanged,
including both real-process invocations, shared graceful-shutdown cases, native
result validation and artifacts. Its stable aggregate check now requires that
job to finish successfully; failure, cancellation, skipped or missing results
fail the aggregate. Ordinary PR/source/browser/live suites remain unchanged.
Retired native fixtures and their component tests remain until separate source
cleanup; no runtime implementation or safety gate is modified by this CI change.


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
Jobs with multiple pytest commands declare their expected invocation labels. The
summary reports any label with no records as `missing_invocations` and marks the
diagnostics incomplete rather than treating records from another invocation as
complete evidence.

These files are diagnostic evidence only. They do not make a test pass,
authorize READY or publication, or replace the test process exit status. A
diagnostic write or summary failure likewise cannot replace the original test
result. The reviewed PR shard weights in `tests/data/pr-test-durations.json`
remain independently maintained and are not updated by this facility.

The AS-001 management self-update/reconnect case also records its custom
operation-transition waits as `management.operation_wait` spans. Each has a
distinct span identity and a bounded `wait_boundary` label; the operation ID
is recorded once admission has supplied it and remains unavailable during the
admission wait. The wrapper delegates to the original `_wait` with the same
predicate, timeout, interval, return value and exception behavior. These records
do not add a diagnostic-completeness assertion to that integration test.

For focused real-run inspection, enable timing and run
`bash scripts/test.sh tests/test_management_self_update_integration.py::test_real_self_update_drains_reconnects_twice_and_replays_original_operation`.
Inspect its native setup/call/teardown outcomes together with preparation,
installer, startup/readiness, operation-wait and cleanup records. A successful
different management case cannot supply missing wait attribution for this named
case. Retain the executed checkout and installed fixture-source identities when
reporting that evidence; a diagnostic span is not an acceptance receipt.

Enabled recorders also deliver bounded human-readable diagnostics to colored
stdout and a separate rotating `diagnostics-*.log` file. Each synchronous emission
owns filtered temporary sinks and removes only those sinks. Pytest's sink cleanup
between cases therefore cannot silently disable later timing logs, and unrelated
application records are not copied into the diagnostic file. Channel failures
remain best effort, retain the original outcome, and produce a bounded warning
through surviving channels. Disabled recording remains silent. Helper verification
returns before recorder creation, and preparation completes its diagnostic output
before printing its final JSON result.

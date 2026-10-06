# Repository-owned merge-operation resumption

Each engine owns a separate merge-operation scheduler. Before serving, it binds
immutably to the repository passed to `start_automation` and installs its resume
handler. Due enumeration, next-deadline selection, interrupted-effect recovery and
bound status snapshots select only that repository. The shared SQLite database
and retained operation identities are unchanged; no migration is needed.

Ownership uses the existing pending-work repository comparison (ASCII case folding
and surrounding ASCII whitespace removal). A foreign operation is rejected again
at the production handler before a GitHub read, trace execution or candidate
processing. Matching operations use their retained repository identity for those
steps, including when another repository has the same PR number.

After a resumption returns or raises, a still-waiting, same-generation operation
whose deadline remains due receives a durable 30-second reevaluation delay. A
future deadline already set by the effect adapter or governor is preserved,
including a shorter future deadline. Completion, operational blocks, supersession,
running effects, newer generations, receipts and throttle retry counters are not
changed by this delay. A 404 or other escaping exception remains a failed execution,
not evidence that the PR merged or the operation completed. Handled strict-refresh
errors retain their deferred trace outcome.

The deadline survives scheduler reconstruction. If its persistence fails, the
failure is logged and that scheduler keeps a local 30-second floor; this fallback
cannot survive a process restart while the store is unavailable. The scheduler
does not classify unknown exceptions into permanent effect failures, invent
receipts, or create a new automatic retry budget. Explicit effect reconciliation
and existing terminal-state policy remain authoritative.

Run `bash scripts/test.sh tests/test_merge_operation_resumption_isolation.py
 tests/test_merge_operation_scheduler.py tests/test_merge_operation_state.py
 tests/test_merge_operation_adapter.py tests/test_dashboard_observability.py`
for the bounded production-resumption, state, adapter and dashboard regressions.

# Separate Issue and PR workers in live durable processing

The daemon starts dedicated Issue and PR worker pools, each with the configured
MAX_CONCURRENT_TASKS workers (default one each). An explicit start_automation
concurrency value applies to each pool. Busy Issue workers cannot consume PR
workers, and busy PR workers cannot consume Issue workers. Dependency update
obligations are handled by the Issue pool. Implementation-slot
admission limits remain shared and unchanged. Each pool retains arrival order
within equal priority. Restart recovery preserves durable generations and
delivery coalescing, including PRs recovered from Codex Cloud after startup.
The dashboard queue snapshot groups PRs first (priority 1) and Issues second
(priority 0); this is a display order, not a cross-pool execution order. Worker
IDs are unique across both pools, and queued work is not an observed execution.

Automatic invalidation, capacity-refill and pending-work processing of a submitted
tracking parent persists each open direct child in the durable invalidation queue
instead of running a child's implementation inline. Duplicate submissions coalesce
and restart preserves the child obligations. Each child still passes ordinary
authoritative readiness, dependency, validation and ownership gates. Explicit
single-parent processing retains its existing child-selection behavior. The parent
reports a deferred hierarchy handoff, not implementation success; a slow child
cannot occupy the parent's worker or refill pass for its entire implementation.
If graceful shutdown begins while parent validation is running, the completed
batch is joined but child handoff is deferred and the parent obligation remains
retryable; shutdown does not create new child obligations.

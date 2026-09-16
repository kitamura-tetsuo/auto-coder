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

# Graceful daemon shutdown

The long-running `process-issues` daemon handles both SIGINT and SIGTERM by
entering an observable draining state. Draining stops candidate admission and
capacity/provider dispatch, preserves queued and newly delivered webhook
invalidations for restart, and waits for already-started local synchronous LLM
or validation work to reach its durable completion boundary. Durable remote
provider work does not delay shutdown. A second SIGINT explicitly force-stops
the drain and is logged separately. Repository Compose services allow a
30-minute stop grace period, and the image's exec-form entrypoint delivers
Docker's SIGTERM directly to Auto-Coder.

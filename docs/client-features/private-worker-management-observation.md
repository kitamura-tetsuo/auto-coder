# Private worker management observation

Externally managed release and beta workers may expose a read-only management
bridge on a Unix-domain socket. Set `AUTO_CODER_MANAGEMENT_SOCKET` to an
absolute path below the channel's `AUTO_CODER_RUNTIME_ROOT` and set
`AUTO_CODER_DEPLOYMENT_GENERATION` to the stable Supervisor's opaque generation.
The runtime directory must already exist, be owned by the worker's local operator,
and be private. The repository must be assigned to the configured release or beta
deployment. If the socket variable is absent, no private listener is created.

The bridge opens no TCP port and runs in the worker process alongside its existing
`AutomationEngine`. It refuses to replace any existing endpoint. The socket is
mode `0600`, its directory is mode `0700`, and shutdown removes the socket only
when it is still the inode created by that worker lifetime.

## Live schema

`GET /v1/observation?nonce=<opaque-ascii>` requires exactly one printable ASCII
nonce of 1 through 128 characters. Schema version 1 echoes it and returns the
captured repository, channel and deployment generation; trace process-run ID and
observation time; independently available installed distribution/source fields;
declared artifact configuration; actual lifecycle, startup reconciliation and
daemon-serving facts; the invocation gate's state, graceful readiness, exact
unsettled count and at most 500 redacted identities; and the existing production
heartbeat's sequence, stage, and age. A read never advances the heartbeat.

`lifecycle.daemon_serving` is narrower than `running`: it becomes true only after
startup reconciliation and subsequent setup have created the required supervised
task set, and becomes false as soon as drain or task exit begins. Responsiveness
therefore does not imply startup readiness or useful progress. Likewise, an aging
heartbeat is evidence of no newer producer heartbeat, not proof of failure.

The existing `/api/status`, `/api/logs`, and `/api/validation-attempts` read-only
projections are also available on this socket, with their existing validation,
redaction, clipping, cursor, retention-gap, and unavailable semantics. This does
not enable those routes on the public webhook application. Every private response
has `Cache-Control: no-store`, `Auto-Coder-Process-Run-Id`, and
`Auto-Coder-Deployment-Generation` headers.

## Terminal schema and limits

Only the actual successful graceful transition to `STOPPED`, after the closed
non-forced invocation gate has no unsettled work, atomically replaces
`last-terminal.json` beside the socket. Schema version 1 records repository,
channel, generation, process-run ID, observation time, bounded safe reason,
lifecycle and gate state, graceful readiness, and unsettled count. Cancellation,
force, startup failure, unexpected task exit, missing evidence, and write failure
do not create a successful receipt. An older receipt retains its own incarnation
identity and says nothing about a later worker.

Observation documents and receipts are limited to 256 KiB, identity lists to 500
entries, and free text to 2,000 characters. A graceful receipt proves only that
locally admitted invocations reached their owned durable checkpoints. A handed-off
provider job may still be running. Neither a live response nor a receipt proves
that the OS process or container exited; the stable Supervisor must observe that
independently.

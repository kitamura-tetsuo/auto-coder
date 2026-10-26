# Management service bootstrap supervisor

The optional management deployment in `compose.management.yml` runs an immutable
bootstrap module as the container's long-lived PID 1. It owns exactly one configured
management-service child. The bootstrap never controls Docker and is upgraded only
by an operator replacing the management image and configuration; a child update does
not update the bootstrap's installed Python environment.

## Manual bootstrap

Create the four management directories shown in the Compose file and place
`config.json` in the read-only bootstrap directory. Set `AUTO_CODER_IMAGE`,
`MANAGEMENT_DIGEST`, and `MANAGEMENT_PORT`, then explicitly start only the management
profile with `docker compose -f compose.management.yml up -d management`. Do not
mount `/var/run/docker.sock`. Worker services and their volumes are not part of this
deployment.

Configuration fixes the instance ID, loopback listener URL, separate absolute child
state, OAuth, and management-operation journal roots, and a closed set of runtime
profiles. Each runtime has a symbolic `reference`, an executable path, its
`sha256:<hex>` content receipt, fixed argument vector, fixed environment, and an
optional drain-acknowledgement path. The initial runtime must name one of those
profiles. Every non-bootstrap runtime also identifies an immutable source root,
its deterministic content receipt, and an independently written READY receipt that
binds that content to the configured runtime reference. Executable and source
receipts are reverified immediately before both launch and recovery. Symlinks are
not supported inside a verified source root because their targets are outside the
closed immutable content receipt.

## Private process contract

`management-child.sqlite3` is a versioned, channel-private store intended for a
future authenticated management service adapter. It is deliberately separate from
OAuth/grant storage and the management-operation journal. The fixed operations are
`SUPERVISE`, `INSPECT`, `REQUEST_GRACEFUL_STOP`, `START_SELECTED`, and `RECONCILE`;
their bounded operation and attempt identities fence durable pre-effect and observed
receipts. The contract reports the Supervisor incarnation, runtime reference,
child generation, and Linux PID plus process-start token. Process liveness and
application readiness are separate: a running PID never asserts OAuth or MCP
readiness. The bootstrap accepts neither OAuth tokens nor a public IPC listener.
An atomic durable generation reservation fences concurrent Supervisor participants;
only the winner can spawn, while a stale contender receives `CONFLICT` or `UNKNOWN`.

A graceful stop sends one cooperative `SIGTERM`. Its JSON drain receipt must match
the exact generation, launch nonce, PID, and Linux process-start token; a file left
by an earlier generation is never accepted. The next runtime is not started
until both actual process exit and the configured child drain acknowledgement are
observed. A deadline reports `DRAINING`; it does not force-kill the child. Stale OS
identity, corrupt or unfinished effects, and externally changed state remain
`UNKNOWN` or `NEEDS_ATTENTION` rather than authorizing another listener.

An initial child may start only when there is no prior or unresolved action and the
configured crash budget permits it. An intentional stop survives Supervisor or
container reconstruction. Following a confirmed candidate launch failure, the
Supervisor records and makes at most one attempt to run the immediately previous,
still receipt-verified runtime. `PROCESS_RESTORED` describes only that new process
lifetime and source binding: it does not roll back OAuth data, the management journal,
provider requests, configuration, or schemas.

Clients should expect a bounded HTTP reconnection gap during replacement and
reconcile with their immutable operation IDs. OAuth/grant and operation-journal
mounts are retained throughout, but their semantic state is outside this
process-lifetime supervisor's authority.


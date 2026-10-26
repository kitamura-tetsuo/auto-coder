# In-container worker supervisor

Release and beta containers use the image-installed `python -m
auto_coder.worker_supervisor` as their long-lived PID 1. The supervisor owns at
most one `process-issues` child and accepts one newline-delimited JSON operation
per connection on the private `/runtime/supervisor/control.sock` Unix socket.
Client EOF never stops it. Supported operations are `INSPECT`, `START`,
`REQUEST_GRACEFUL_STOP`, and `RESTART_WORKER`; mutating requests require a
1–128-character `operation_id`. Reusing that ID with a different operation,
expected identity fence, or intended runtime returns `CONFLICT` without changing
the original accepted request. An identical replay returns or reconciles only
its durable result.

## One-time deployment and startup

An operator must manually rebuild and deploy the Supervisor/bootstrap image and
the updated `compose.channels.yml` once (using `docker compose` on the host).
The containers use distinct persistent `/runtime` and `/workspace` mounts,
unchanged repository routing, and no Docker socket, host cgroup mount, or
privileged setting. The supervisor cannot install itself, update the image, or
operate Docker. The authentication/network bridge is a separate component.

On the first boot **without an existing Supervisor state file**, PID 1 attempts
one bootstrap worker start and stores its operation before spawning. When a
state file already exists, including after a deliberate stop or crash, it does
not automatically resurrect a child. Thus the startup crash-retry budget is one
attempt per new state volume, not an unbounded crash loop. An operator can
disable first-boot launch with the immutable deployment configuration
`AUTO_CODER_SUPERVISOR_AUTO_START=0` (primarily for isolated maintenance/tests).
Normal starts always use the fixed `process-issues --repo ... --log-file ...`
arguments, with the configured repository, channel, runtime/workspace roots,
management socket and a fresh generated deployment generation.

The one-time image configuration supplies the immutable instance ID, channel,
repository, runtime and workspace roots, and bootstrap interpreter. The
schema-versioned `/runtime/supervisor/state.json` is atomically replaced and
fsynced on the channel-private persistent volume. It records the selected
runtime ID, process PID and Linux start token, deployment generation, actual
worker-owned process-run ID once observed, immutable READY selection, launch
intent, effect-generation identifiers, operation payload hashes and final
receipts. Supervisor `incarnation` changes with every PID-1 start; a persisted
unresolved effect remains fenced across that change.

## Runtime identity and inspection

Staged runtime IDs are resolved only by scanning the configured private version
root and independently revalidating exactly one immutable `READY.json` through
the worker-version verifier. Its interpreter, environment, runtime profile,
installed runtime ID, source revision, distribution version, locked-input hash
and receipt content identity must remain unchanged. Immediately before every
actual staged launch—including after a held RESTART drain—the original accepted
selection is revalidated. An absent, changed or ambiguous READY returns
`UNAVAILABLE`; there is never a fallback to bootstrap or an in-place update.

Each live `INSPECT` obtains a fresh nonce-bound management observation over the
worker Unix socket, verifies the peer process PID and Linux lifetime before and
afterward, and compares repository/channel/generation and the genuine
`process_run_id`. A staged runtime binding is `VERIFIED` only while the original
READY still independently validates and the live artifact source/version match
that exact retained lock/content identity. `CONFLICT` or `UNAVAILABLE` clears
the previous runtime binding; separately known process facts remain readable.
An image-installed bootstrap worker may expose its genuine run ID, but it never
invents a staged lock/READY identity (`binding_status=UNAVAILABLE`).

## Graceful stop, replay, and recovery

Stopping sends at most one SIGTERM via a Linux pidfd to the specific recorded
process after checking its lifetime and all supplied identity fences. The
worker's genuine `last-terminal.json` must match repository, channel,
generation, run ID and successful graceful closure, **and** the exact OS
lifetime must have exited before reporting `STOPPED`. It never sends SIGKILL,
second SIGINT or a process-group signal. Held drains remain `DRAINING` or
`UNKNOWN`; successful `RESTART_WORKER` then permits exactly one revalidated
successor. Losing a response or restarting PID 1 does not authorize replay to
repeat an ambiguous effect. An unresolved acceptance/launch or a crashed child
without terminal evidence remains `UNKNOWN`/`NEEDS_ATTENTION` and prevents
another start until deliberate operator reconciliation.

When Docker sends PID 1 SIGTERM for a manual container stop, the Supervisor
requests the same cooperative drain and waits for the child to exit; it does not
force the worker to meet an observation timeout. Host-side container stop
policies still determine the ultimate shutdown deadline, outside this process's
authority. Supervisor logs go to colored-capable stdout and to rotating
`/runtime/supervisor/supervisor.log` without logging arbitrary request payloads.

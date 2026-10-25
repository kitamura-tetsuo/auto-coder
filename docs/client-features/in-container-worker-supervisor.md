# In-container worker supervisor

Release and beta containers run a small bootstrap supervisor as PID 1. The
supervisor owns at most one `process-issues` child, remains available while the
child is stopped or draining, and accepts one newline-delimited JSON operation
per connection on the private `/runtime/supervisor/control.sock` Unix socket.
Closing a client connection never ends the Supervisor. Supported operations are `INSPECT`, `START`,
`REQUEST_GRACEFUL_STOP`, and `RESTART_WORKER`; every mutating request needs a
bounded `operation_id`. Reusing an ID with a changed operation or identity fence
returns `CONFLICT`, while an identical replay returns its durable receipt.

The one-time image configuration supplies the immutable instance ID, channel,
repository, runtime and workspace roots, and bootstrap interpreter. State is
stored at `/runtime/supervisor/state.json`, on the channel-private persistent
volume. A generated deployment generation and the private management socket are
passed to each child. Operators must rebuild the bootstrap image to update the
supervisor; it cannot update itself or select commands supplied by a caller.

Staged runtime IDs are resolved only by scanning the configured private version
root and independently revalidating the matching immutable `READY.json`; invalid
or missing evidence never falls back to the bootstrap interpreter. The worker's
`process_run_id` is unavailable until a fresh nonce-bound management observation
joins it to the recorded OS lifetime and deployment generation.

Stopping sends exactly one SIGTERM to the recorded PID after confirming its
Linux process start token and all supplied identity fences. It never sends
SIGKILL or signals a process group. Until both the matching worker terminal
receipt and OS exit can be reconciled, the result remains `DRAINING` or
`UNKNOWN`; ambiguous reconstructed state and unresolved durable effects fence
new starts. The supervisor does not use Docker APIs,
the Docker socket, host namespaces, privileged mode, or cgroup administration.

# Management operation journal

The management operation journal is a SQLite store rooted in an explicitly
configured persistent **management state root**, separate from worker runtime
roots, workspaces, OAuth state, and merge-operation state. Operators own its
backup; restoring an old backup never makes an external effect safe to repeat.

Admission durably records normalized intent before returning an operation ID.
The idempotency namespace includes issuer/resource, authenticated project/UID,
OAuth client, managed target, and client key. Identical replay returns the
original operation and accepted authority; changed intent conflicts. Refreshed
credentials do not update that authority. Tokens, authorization codes,
arbitrary commands, unrestricted paths, and raw provider output are excluded.

A target has one lane. `ACCEPTED`, `RUNNING`, `RECONCILING`, and
`NEEDS_ATTENTION` retain it. Only an evidence-bearing transition to `SUCCEEDED`,
`ROLLED_BACK`, or `FAILED` releases it. A reevaluation consumer may complete
when a worker durably accepts the request without waiting for the Issue or PR;
that receipt is not the eventual business outcome.

Effects become `SENT_UNKNOWN` before dispatch and only a correlated consumer
observation can make them `CONFIRMED` or `REJECTED`. Timeout, restart, token
expiry, an empty response, or a missing object proves no terminal result.
Resolution is revision-fenced; there is no expiry, takeover, eviction, or
force-clear. A lost admission response is recovered by repeating its key and
intent. A changed artifact conflicts, while a lost container response retains
the lane until correlated observation.

Admission is not authorization. Consumers authorize current requests and
validate the external producer behind receipts. Queries expose only bounded,
typed safe projections and cursor pages; operation IDs, phases, targets, and
safe categories use the existing Loguru sinks.

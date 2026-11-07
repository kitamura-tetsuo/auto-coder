# Retained PR publication diagnostics

`auto_coder_pr_diagnostics` is an authenticated, read-scoped management MCP
tool for inspecting the retained processing evidence for one PR on one configured
worker target. Its closed request schema is `target_id`, positive `pr_number`,
optional `limit` (1–100), optional numeric `cursor`, and the first page's
64-character `expected_revision` (required when `cursor` is nonzero). The target resolves
through operating policy to the fixed instance, repository and channel; clients
cannot supply a repository, path, URL, command, SQL statement, or store name.
The projection is private and is not registered on the anonymous public
diagnostic API.

The response schema version is 1. It carries the authenticated instance,
repository/channel, worker process run, deployment generation and Supervisor
generation supplied by the existing management observation chain. Installed
artifact facts and `VERIFIED`, `CONFLICT`, or `UNAVAILABLE` staged-runtime
binding remain separate. A genuine bootstrap-worker observation can therefore
carry useful retained facts while binding is `UNAVAILABLE`; an incarnation
change makes the read conflicting rather than current.

The `sections` object independently reports `review_cycle` (including the
active claim and accepted round/closure publication state),
`effective_decision`, `review_effects`, and repository-owned `pending_work`.
Every section names its producer and availability. `COMPLETE` with an empty
`records` list means the store was read successfully and had no matching PR;
`ABSENT`, `UNAVAILABLE`, `UNSUPPORTED`, `CORRUPT`, and `CONFLICT` do not mean
the operation completed. Records retain their own heads, epochs, rounds,
attempts and revisions. The tool does not synthesize an atomic snapshot or an
overall readiness verdict, and an inconsistent identity remains visible rather
than being joined to a different record.

Reads have bounded store sizes, record counts, response bytes, lock waits and
deadlines. Free text is redacted before clipping. Pagination cursors remain
bound to the authorized target and PR by the MCP request; changed source
revisions are visible on subsequent pages. Each first page supplies an
`evidence_revision`; if any producer changes before continuation, the response
sets `evidence_changed` and reports affected complete sections as `CONFLICT`
instead of silently shifting records. Review-cycle `rounds` and `closures` are
bounded by the same selector and carry their own truncation and next-cursor
fields, so a large cycle cannot discard independently available sections.
Raw owner tokens, accepted payloads,
credentials, filesystem paths, prompts and source contents are never returned.
Private responses use `Cache-Control: no-store`.

Inspection does not create a missing store or lock directory, acquire or adopt
an execution owner, take a processing claim, acknowledge an effect, reconcile
publication, increment an attempt or heartbeat, requeue work, call a provider,
or mutate GitHub. Lock contention is reported as unavailable within the read
bound. This new read path emits no new processing trace and changes no existing
trace schema or dashboard outcome, so it is observability-neutral.

## Operator sequence

1. Call `auto_coder_status` for the configured target and retain its exact
   instance, repository/channel, process run, deployment and Supervisor
   generations plus runtime-binding result.
2. Call `auto_coder_pr_diagnostics` for the PR and preserve every producer's
   availability, identity/revision and records.
3. Call `auto_coder_validation_attempt` for the exact attempt named by retained
   evidence. If a management operation exists, call
   `auto_coder_operation_get` with its exact operation ID and target.
4. Separately observe GitHub review and thread state, then compare identities
   and receipts rather than inferring one source from another.

A resolved GitHub thread, expired wait deadline, absent retained record,
accepted reevaluation, or available diagnostic is not proof that publication
completed, the PR may merge, or a retry is safe. `UNAVAILABLE` without a
receipt is an evidence gap: do not guess a receipt ID, create a fresh mutation
key, force a retry, or restart the worker to manufacture an outcome.

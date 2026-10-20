# Durable local PR repair admission

Selected local CI-failure and merge-conflict repairs are offered to
`LocalJobStore` as exact repository, PR, head, base, repair-kind and backend
operations. The producer repeats the authoritative open/unmerged and branch
identity read immediately before admission. CI admission additionally retains
the complete terminal failed-check execution identities; an affirmative
conflict observation is independent of empty CI evidence.

The ordinary PR processor enters this boundary after selecting either its
local CI route or its definitive local conflict route. An accepted envelope is
an asynchronous handoff, so the selector does not continue into the former
synchronous checkout/model operation. Re-entry recovers the same envelope;
failed or blocked admission is deferred without falling through to local or
cloud execution.

The SQLite transaction returns an identical retained envelope for an exact
replay and refuses a competing unsettled CI, conflict, or review-correction
edit owner for the same PR. A changed head/base, fork origin, non-local route,
invalid owner allowance, incomplete evidence, contradictory replay, or storage
failure is a deferral rather than permission to fall back to another backend.
Pending, running, and downstream-publication envelopes continue suppressing
competing work. Historical envelopes are retained when branch identity later
changes; they do not provide publication authority for the newer branch.

Admission does not invoke a backend or establish publication, CI success,
review clearance, mergeability, merge completion, or slot release. A running
job whose provider-entry outcome is unknown remains recoverable and cannot be
converted into a fresh execution merely because a controller restarted.

Focused coverage is in `tests/test_local_pr_repair_job.py` and exercises
reopened stores, genuinely concurrent offers, branch and origin changes,
CI-state distinctions, conflict admission, competing review work, and failed
persistence.

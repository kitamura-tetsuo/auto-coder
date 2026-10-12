# Muse background-work observation

Muse background work is represented as correlated evidence, not as one
completion flag.  `MuseObservationBinding` ties every fold to the selected
backend, exact provider session, accepted execution incarnation, upstream
attempt, and prepared workspace.  The normalized observation independently
reports foreground state, live or terminal tasks, terminal outcomes and result
references, queued and active turns, incorporated-result IDs, assistant-output
association, provider provenance, and continuation readiness.

The fold consumes frames after the production MSP JSON decoder.  It tolerates
duplicate and out-of-order delivery by task revision and receive order; MSP
cursor strings are retained as opaque provenance and are never sorted.  A
foreign session is ignored.  An older revision cannot roll back a task, while
conflicting terminal outcomes require reconciliation.  `task/list` is only a
live nonterminal inventory: an empty inventory or a missing prior row leaves
the task unresolved until attributable terminal evidence arrives.  Unavailable
views, malformed workload rows, and identity contradictions likewise remain
non-authorizing.

Continuation readiness distinguishes an already queued or running native turn
from controller readiness.  Controller readiness is possible only after all
known task results are delivered, foreground work is terminal, no view gap is
open, and the runtime positively advertises controller ownership.  Silence
does not establish controller ownership.  Final assistant text retains its
turn association and is withheld while work or result delivery is outstanding;
failed and cancelled task history is retained for a later turn to interpret.

Runtime support is capability-earned rather than fingerprint-allowlisted.  The
recorded positive profile is Muse Code `1.4.1-R4503.1`, with `taskList`,
`sessionView`, `taskResultDelivery`, and an unambiguous native or controller
`continuationOwnership` capability.  Harmless additive fields do not invalidate
that profile.  A host missing one of those decisive facts produces a precise
unsupported reason and cannot authorize continuation.  This declaration must
be corroborated for a deployed binary by replaying its real MSP initialization
and background lifecycle; a version string alone is not authority.

`persist_muse_observation` writes normalized tasks and readiness only through
the current `LocalJobStore` executor fence.  It rechecks session, backend,
execution lineage, upstream attempt, and workspace before accepting anything.
It does not start or stop a host, submit a follow-up, recreate work, publish,
or settle domain ownership.

Run `bash scripts/test.sh tests/test_muse_background_observation.py
tests/test_local_job_continuation.py` for adverse transcript ordering, missing
inventory, contradictory terminals, native wake, capability compatibility,
and exact fenced persistence coverage.

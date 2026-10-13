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
does not establish controller ownership. Final assistant text retains its turn
association and the exact result frontier incorporated by that turn. A
foreground summary cannot inherit results delivered only to a later native
continuation, and is withheld while work or its own result delivery is
outstanding; failed and cancelled task history is retained for a later turn to
interpret.

Runtime support is behavior-earned rather than fingerprint-allowlisted. The
recorded positive profile is Muse Code `1.4.6-R6000.1` (MSP server `1.4.6`). Its
dedicated live test starts that exact released binary against a loopback model
transport, makes a real Bash tool wait on a gate, backgrounds it with
`task/background`, observes it in `task/list`, lets the foreground turn finish,
then releases the gate and observes the tool terminal, result delivery, native
continuation turn, and `session/read` replay. The binary download is pinned by
release and SHA-256 in `.github/workflows/muse-live-tests.yml`. Harmless additive
fields do not invalidate a profile, but initialization metadata and a version
string alone cannot earn support. A caller must supply all decisive facts from
one correlated runtime lifecycle; missing facts produce a precise unsupported
reason.

`persist_muse_observation` writes normalized tasks and readiness only through
the current `LocalJobStore` executor fence.  It rechecks session, backend,
execution lineage, upstream attempt, and workspace before accepting anything.
The durable snapshot preserves native-active versus controller-ready authority,
turn identities, revisions, delivery associations, incorporated-result IDs,
and unresolved evidence so restart recovery cannot flatten those states.
It does not start or stop a host, submit a follow-up, recreate work, publish,
or settle domain ownership.

Accepted local Issue jobs install that fence around the normal Muse client.
The production MSP consumer folds and checkpoints notifications, reads the live
`task/list`, waits for attributable native result delivery when background work
exists, and reconciles the released host's inline `session/read` history before
returning a candidate output. Late acknowledgements and duplicate starts cannot
resurrect a terminal continuation turn. A tool transport status of `completed`
remains an unknown domain outcome unless attributable exit evidence proves
success or failure.

Run `bash scripts/test.sh tests/test_muse_background_observation.py
tests/test_local_job_continuation.py` for adverse transcript ordering through
the production decoder, missing inventory, contradictory terminals, native
wake, compatibility, and exact fenced persistence coverage. With the pinned
host installed, run `AUTOCODER_MUSE_CONFORMANCE_BIN=/path/to/muse bash
scripts/test.sh -m muse_live tests/test_muse_background_live.py` for the
executable positive profile.

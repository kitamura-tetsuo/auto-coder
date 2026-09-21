# Durable explicit retry dispatch handoffs

An explicit implementation retry that already owns its durable request,
logical attempt, and semantic generation can claim exactly one downstream
creation operation. The claim binds the repository, Issue, request, attempt,
generation, selected route, named backend, non-secret route configuration,
and a stable creation identity before a local process or remote submission is
allowed to start. Independent processes reconstruct the same claim rather
than creating duplicate work.

Claims distinguish definitely-not-started, accepted, indeterminate, and
completed outcomes. A crash with only a claim is indeterminate and suppresses
replacement work. Only authoritative evidence that creation definitely did
not start reopens the same creation identity. Accepted receipts are immutable;
tracking completion is recorded separately so missing CloudRun, cloud.csv, or
slot projections can be repaired around the same task without resubmission.
Provider tracking and full handoff completion are separate checkpoints: the
latter is set only after the exact accepted CloudRun, authorized current
provider binding, and logical-slot membership have all been confirmed.
The machine-readable record distinguishes `accepted-current`,
`accepted-tracking-incomplete`, and `accepted-historical` projection
dispositions and retains the exact binding that was current when creation was
claimed.

Routes that use numeric Issue attempts allocate the number once, above both
current authoritative evidence and every retained allocation for that Issue.
Replay and definitely-not-started recovery reuse it. The handoff query API
exposes the complete identity, outcome, receipt, diagnostic, and tracking
state without treating any of those states as proof of implementation
completion.

The journal intentionally rejects credential-shaped route configuration. It
is consumed at the local, Jules, Claude Routine, and Codex Cloud creation
boundaries. Ordinary and high-score cloud selectors preserve the exact
authority while resolving configured aliases; boolean-only retry dispatch is
refused rather than converted into a new attempt. Replay of accepted remote
work repairs tracking around the retained receipt, while replay of a completed
local invocation does not invoke the agent again.

Codex Cloud retry acceptance conditionally promotes that retained predecessor.
The accepted-receipt commit, later-accepted check, and current-pointer write use
one cross-process coordination fence. Consequently, an unknown owner is never
overwritten and an older accepted repair cannot win after a later accepted
handoff. The prior binding remains in the handoff history; replacement does not
cancel or fabricate provider liveness.

The provider creation boundary reloads Codex retry authority from the durable
request store and requires the repository, Issue, generation, logical attempt,
owned state, and ownership reference to match the supplied handoff. Provider
predecessor attribution is captured durably when implementation ownership is
acquired, before later pointer updates can race with dispatch. Jules and Claude
Routine retry writers use the same conditional promotion fence as Codex, so a
later accepted cross-provider handoff cannot be overwritten by an older writer.
If Codex acceptance reached the CloudRun journal before the retry receipt, replay
recovers that exact receipt from the matching attempt and completes projection
without another provider submission or numeric allocation.
Conversely, if the retry receipt is writable but the accepted CloudRun update
fails, the receipt retains the task and execution environment so replay can
upgrade the indeterminate run claim without resubmission. Migrated accepted
requests that predate predecessor capture may confirm an already-matching
current pointer, but cannot replace a missing or different pointer.

A later accepted request may replace an earlier accepted winner derived from
the same Issue history even when both originally captured the same predecessor.
Projection acknowledgement rechecks both latest-accepted order and the exact
current pointer under the coordination fence, so a delayed acknowledgement
cannot restore a superseded receipt to `accepted-current`.

Explicit processing consumes this typed receipt rather than comparing the
pointer with a pre-dispatch snapshot. Thus same-request replay may complete
with an unchanged pointer, while an unrelated changed pointer cannot establish
success. Accepted receipts whose joined checkpoint is unfinished are placed on
the daemon's durable pending-work service at dispatch and startup. Recovery
uses the receipt's original request, attempt, generation, numeric attempt,
backend, environment, and base provenance; it performs no provider creation,
attempt allocation, route selection, or Issue review. A superseded or retired
receipt remains historical instead of becoming current again.

The pending-work consumer performs the repair itself: it reconstructs a
missing accepted run from retained launch provenance and conditionally promotes
only the predecessor captured by the retry admission record (or an earlier
accepted retry). Missing provenance and unrecognized bindings remain pending.
This daemon path therefore makes progress after restart or transient storage
failure without requiring adapter replay, while preserving concurrent PR
associations and newer accepted ownership.

This recovery does not choose providers, change creation quota or fallback
policy, cancel old work, imply provider execution completed, or replace
ordinary non-retry dispatch semantics. A separate operator retry remains new
intent; automatic recovery is bookkeeping for the original accepted request.

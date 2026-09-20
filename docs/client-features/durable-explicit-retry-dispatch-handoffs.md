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

Routes that use numeric Issue attempts allocate the number once, above both
current authoritative evidence and every retained allocation for that Issue.
Replay and definitely-not-started recovery reuse it. The handoff query API
exposes the complete identity, outcome, receipt, diagnostic, and tracking
state without treating any of those states as proof of implementation
completion.

The journal intentionally rejects credential-shaped route configuration. It
does not activate a controller or CLI retry path, choose providers, change
quota or fallback policy, cancel old work, or replace ordinary dispatch
semantics.

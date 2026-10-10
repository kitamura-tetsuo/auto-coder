# Local Issue PR publication recovery

After local implementation, a clean working tree does not imply that a pull
request has been published. If there are no new changes to commit, Auto-Coder
fetches the intended PR base from origin and checks the effective diff from that
base's merge point to HEAD. When existing committed changes remain, it pushes
the work branch through the centralized Git helper and creates the missing PR
using the normal Issue PR publication path. An existing PR is reused and retains
implementation ownership just like a newly created PR.

Failed fetches, diff inspection, or pushes never authorize PR creation. If the
branch has no effective diff, Auto-Coder reports that a PR cannot be created and
leaves the Issue open; it does not manufacture an empty PR. Implementation prose
such as "already implemented", "invalid", or "closed" never closes an Issue or
produces a fabricated close/comment action. The PR's closing directive remains
the normal completion mechanism when the implementation is merged.

The existing `issue.local-commit-push` stage reports `COMPLETED` after existing
changes are pushed, `FAILED` on publication preparation errors, and `SKIPPED`
for an empty diff. An empty diff also emits `issue.pr-publication` as `BLOCKED`
with its reason. PR creation/reuse retains the existing publication-stage
outcomes and trace schema. Regression coverage is in
`tests/test_issue_processor.py::TestKeepLabelOnPRCreation`.

Completed asynchronous Issue jobs use a stricter job-scoped finalizer. It
validates the exact result artifact, execution incarnation, retained workspace,
source commit, and work branch before committing. Commit, push, PR, and owner
association each receive a durable effect checkpoint. On restart, completed
effects are reused and only missing effects resume; the editing backend is never
invoked by this continuation.

Pushes are confirmed against the remote branch before PR publication. PR
creation is always followed by authoritative branch lookup, so a lost create
response reuses the remotely created PR rather than creating another one.
Unavailable or contradictory lookup evidence remains pending, as do failed or
ambiguous pushes. `CANNOT_FIX` and authenticated no-change results settle with
distinct dispositions and cannot manufacture a PR. Implementation ownership is
associated only after the exact closing PR is confirmed and otherwise remains
reserved for normal lifecycle recovery.

Regression coverage for the asynchronous boundary is in
`tests/test_issue_job_finalizer.py`.

The workspace checkpoint also binds the exact implementation-owner incarnation
and generation, the controller checkout's authoritative publication remote, and
a complete filesystem identity (content, mode, and symlink state). Finalization
refuses a replacement owner or any post-checkpoint workspace drift. A commit
that completed immediately before a checkpointing crash is recovered only when
it is the clean, direct child of the pinned source and still represents that
complete filesystem identity.

PR reconciliation uses a strict all-state head-branch lookup that propagates
API failures. Once creation has been requested, a missing or unavailable lookup
cannot authorize another create request; recovery waits until the original open
or closed PR can be authoritatively identified.

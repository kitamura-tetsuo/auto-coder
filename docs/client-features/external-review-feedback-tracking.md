# External review feedback tracking

Unresolved PR review comments without Auto-Coder finding headers receive a
controller-owned canonical `blk_...` identity when ordinary review-thread repair
is prepared. The PR-scoped ledger retains their native comment identity, original
text, and root thread/comment aliases. Replaying after a restart, comment edit,
or head change preserves the ID and original scope. Distinct comments remain
distinct even when their text is identical. An actionable reviewer reply gets
its own identity without inheriting root-thread closure authority.

Both explicit-local corrections and existing cloud-session follow-ups carry the
IDs and retained scopes. Mixed managed and external findings share the cloud
repair bundle, so a managed finding cannot suppress an external comment.
Existing per-comment delivery receipts continue to suppress duplicate delivery.
Unavailable ledger state prevents dispatch rather than sending untracked work.
Resolved, empty, and truncated external threads are not admitted by this intake.

Tracking is not reviewer authentication or Issue-contract authoring. Intake
preserves the review text without inventing requirement IDs, an Objective, or a
regression oracle. The linked Issue's explicit Requirements remain authoritative.
Human threads retain the existing merge gate and automatic-closure restrictions;
ID assignment or repair dispatch never resolves a thread.

The existing `pr.repair-delegation` stage reports dispatch/defer/failure through
its existing fields. No new processing origin, provider route, merge authority,
or trace schema is introduced. Canonical identity admission happens within that
repair path and is not a separate executable resumption task.

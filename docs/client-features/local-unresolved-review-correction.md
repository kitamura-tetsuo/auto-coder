# Local unresolved-review correction

Explicit-local pull requests now consume the `LOCAL_REQUIRED` unresolved-review
route in ordinary PR processing. Auto-Coder captures actionable comment identities
and the authoritative exact head, builds a correction prompt from those comments
and the linked Issue contract, and invokes only synchronous candidates from the
target repository's effective ordinary backend policy. Alias resolution, candidate
order, model and options therefore use that repository's overrides rather than a
process-global cached configuration. Task-only providers are never selected as a
local fallback.

The correction runs in a detached worktree at the captured PR head. A SQLite claim
serializes execution for the whole pull request and retains executing,
indeterminate, publication-pending, no-change, and awaiting-validation states so a
restart cannot silently duplicate an uncertain model invocation. Commits publish
only to the existing same-repository head ref with an exact remote-SHA lease; a
newer remote commit therefore leaves the correction recoverably pending rather
than being overwritten. A retained commit is reconciled with the remote branch and
republished without rerunning the model, definitely-not-started work can be
readmitted, and a completed attempt does not absorb later distinct feedback. Local
execution and publication never resolve a review thread or count as merge success:
independent current-head validation remains the owner of that decision.

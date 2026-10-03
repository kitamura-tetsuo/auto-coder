# Local unresolved-review correction

Explicit-local pull requests now consume the `LOCAL_REQUIRED` unresolved-review
route in ordinary PR processing. Auto-Coder captures actionable comment identities
and the authoritative exact head, builds a correction prompt from those comments
and the linked Issue contract, and invokes only synchronous candidates from the
target repository's effective ordinary backend policy. Alias resolution, candidate
order, model and options therefore use that repository's overrides rather than a
process-global cached configuration. Task-only providers are never selected as a
local fallback.

The same route handles freshly published adversarial `NEEDS_FIX` and
`NEEDS_TESTS` reports and saved same-head report replay. It requires the
authoritative explicit-local PR declaration and the still-current validated
head. Only unresolved roots matching the report's actionable feedback are
submitted; an implementer's addressed claim does not suppress a root that
independent validation still finds defective. Existing stable feedback identities
and the durable local claim prevent duplicate execution on replay. Cloud-owned
PRs retain their existing provider follow-up route; missing cloud ownership alone
does not authorize local repair.

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

The production repair-allowance ledger is acquired before the local claim or any
workspace mutation. Unreadable allowance state fails closed, while a temporary
absence of a synchronous backend remains definitely not started and retryable.
Backend `CANNOT_FIX` output is retained as terminal failure rather than successful
no-change completion. Successful no-change and published completions proceed to
independent same-head revalidation. Ordinary unstaged model edits are staged by the
controller; if staging or committing fails, the detached workspace path is retained
with the durable attempt for recovery.

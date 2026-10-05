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

An existing actionable same-head `NEEDS_FIX` or `NEEDS_TESTS` verdict routes
to repair instead of another full validation, including an explicit `--force`
run. A pending local correction generation takes priority over full validation.
An explicit changed-contract adjudication can still invalidate the old verdict.

Saved reports select existing unresolved findings by their exact native thread ID
and `STILL_VALID` disposition, including strong-audit roots. Reworded summaries
need not repeat the original finding body. `ADDRESSED`, `INCONCLUSIVE`, and
conflicting dispositions do not authorize repair. Reports without a disposition
for the target retain the existing substantive-text matching rule. A local
routing or admission failure propagates a failed processing result; accepted or
pending correction work remains deferred.

The correction runs in a detached worktree at the captured PR head. A SQLite claim
serializes execution for the whole pull request and retains executing,
indeterminate, publication-pending, no-change, and awaiting-validation states so a
restart cannot silently duplicate a live or uncertain model invocation. Execution
ownership records the controller PID, kernel boot ID, and process start ticks.
At the next PR repair admission, a missing process, changed process identity,
changed boot, or zombie/dead process proves interruption and permits atomic
readmission with a new incarnation. Elapsed time alone never expires a claim.
Live owners, unreadable process evidence, and historical records without owner
identity remain blocked. Existing databases gain nullable ownership columns
without discarding their attempts; upgrading does not guess the owner of an old
`executing` record. Such historical records require operator reconciliation.

The detached workspace path is checkpointed before backend entry. Interrupted
uncommitted output is preserved in that workspace, while a retry starts from the
captured PR head in a fresh worktree. A retained committed result resumes
publication without another model call. The existing allowance generation is
reused for interrupted work and completed on confirmed recovered publication;
independent validation is still required. Atomic SQLite admission and incarnation
fencing prevent concurrent replays and stale-owner state writes. Recovery occurs
on the next normal processing pass, rather than a separate background timer.
Commits publish
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
no-change completion. A no-change completion reports that verification was not
performed and suppresses another reviewer invocation on the unchanged commit.
A later new commit can receive scoped verification of the still-pending targets.
Published corrections proceed to scoped independent verification after the
authoritative PR head is refreshed. Ordinary unstaged model edits are staged by the
controller; if staging or committing fails, the detached workspace path is retained
with the durable attempt for recovery.

A completed allowance generation is never reused as delivery authority for a
new attempt. Its pending-revalidation state instead sends ordinary PR processing
through independent verification of only its unsettled covered roots, even when
unresolved threads would otherwise stop that pass. After confirmed review publication, exact-current-head dispositions
settle only the original covered root identities: `ADDRESSED` clears that blocker
and `STILL_VALID` charges one failed generation. Missing or inconclusive
observations retain the outstanding generation. Validation must examine the
published correction or a descendant; unrelated heads cannot settle it. A later
repair then receives a fresh allowance generation. An unfinished generation for a
different attempt cannot authorize a new invocation.

An allowance-delivery failure before entering the backend leaves the local claim
`not_started`, with `executed=False`, so a later pass can retry. Exceptions after
backend entry remain indeterminate and continue suppressing duplicate execution.
The ordinary repair-delegation trace includes `local_phase=awaiting_validation`
when independent validation is due; settlement failures remain explicit failed
repair-delegation events rather than successful corrections.

Retained material test-oracle threads match newly validated feedback by their
stable `TOG` identity as well as exact text. Changed reproduction wording does
not discard an existing open gap: the correction uses the current validated
instructions while preserving the original root's durable feedback identity.
Different gap identities remain excluded even when their prose is identical.

When a completed generation requires verification, its original unsettled roots
are the entire review scope, even if GitHub displays them as resolved.
Auto-Coder retrieves complete threads, matches durable feedback identities, and
authenticates the reviewer. Unrelated findings and settled roots are excluded.
The read-only prompt requests only per-target `ADDRESSED`, `STILL_VALID`, or
`INCONCLUSIVE` dispositions; it does not request a new PR-wide review.

A separate SQLite checkpoint reserves the reviewer invocation by generation and
head before model entry. Restarts reuse its retained response; an interrupted or
indeterminate invocation is reported without repeating it on the same commit.
Publication is separately reserved and reconciled against the exact native
review receipt, preventing blind resend after an uncertain publication. Pending
execution diagnostics and completed reports have separate publication receipts.

The GitHub native review uses a distinct local-repair-verification marker and
cannot supersede the ordinary adversarial verdict or authorize merge. Its
`Pending local corrections NOT verified` section lists each missing, truncated,
unauthenticated, omitted, inconclusive, or failed target with its blocker identity,
available thread identity, and reason. Such targets remain pending and are never
presented as `STILL_VALID` merely because verification failed. Partial results
settle only targets actually verified, after confirmed publication and a current
head check. Resolved UI state alone does not clear an allowance.

The repair-delegation dashboard stage exposes the generation, examined head,
unverified target count and reasons with `effect=local-validation-scoped`.
Incomplete verification is `BLOCKED`; a completed scoped check is `COMPLETED`
but remains a deferred PR-processing result rather than merge approval.
Publication or settlement failures are explicitly `FAILED`.

Proven owner-exit recovery emits `pr.local-repair-recovery` with a deferred
outcome, attempt identity, owner PID, next phase, retained workspace, and reason.
The generic dashboard detail view shows this recovery separately from repair
completion; recovery never claims that findings are resolved.

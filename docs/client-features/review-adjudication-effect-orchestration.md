# Review adjudication effect orchestration

Every normal merge-gate pass for a PR re-derives the current applicability of
each registered review-adjudication context (see "Revision-bound review
adjudication protocol") from durable ledger state plus a fresh read of the
root findings' text, independently of any single process's in-memory cache,
and applies the owned effect of whichever decision is currently the sole
graph tip. This runs unconditionally, alongside the existing stale-review-thread
rollback retry, regardless of CI or adversarial-validation state this pass.

An applicable `UPHOLD/FIX` decision routes its exact rationale and the
original finding text into the PR's normal bounded repair path, reusing the
same durable provider/task association used for ordinary unresolved-thread
repair delegation: an existing supported Codex Cloud task, or another
provider's existing follow-up route when eligible. No new task, local branch
repair, or provider fallback is created, and the target thread is never
resolved by this path; unsupported or indeterminate routing stays visibly
pending. The routed repair prompt never repeats the raw
`<!-- auto-coder-review-adjudication:v1 -->` envelope: any comment carrying
that literal structural marker, authorized or not, is excluded from the
generic reviewer-feedback text this same delegation path would otherwise
forward, so a decision reply is never misdelivered as free-form prose.

An applicable `OVERRULE/NO_CHANGE` decision retires only the target finding's
owned projections. The target GitHub thread is resolved with an auditable
reply distinct from the independent addressed-claim resolver
(`auto-coder-review-adjudication-overruled:v1`, separate from
`auto-coder-review-thread-resolved:v1`), re-verified against the current
decision immediately before the resolve mutation. When the finding is a
persisted material test-oracle gap (identified by the `Gap identity` marker
embedded in the finding by `format_test_oracle_gap_comment`), the gap is
marked `INVALID` with adjudication provenance -- but only when no other live
(non-overruled) finding still embeds that same gap identity, so one shared
gap is never cleared while another contributing finding remains open.

A later decision that supersedes, conflicts with, or revokes the authority
behind an already-applied `OVERRULE` reverses only this same context's owned
projections -- unresolving the GitHub thread and reopening the gap it
retired -- independently of whatever the new current disposition is,
including a fresh `UPHOLD`. A failed reversal is recorded as
`reconciliation-required` rather than silently treated as complete.

Every applied or attempted effect is journaled durably, keyed by the
context's identity plus the exact decision/head/contract generation it was
computed for, so an unchanged decision set is never re-applied and repair
delivery is retried only while unconfirmed. A context with an outstanding
(non-terminal) effect forces this pass past the ordinary same-head
adversarial-validation suppression, the same way an explicit `--force` run
does, so a newly effective decision reaches a fresh revalidation without a
no-op commit or full-repository polling.

Each applied effect is recorded as a `pr.review-adjudication-effect`
processing-trace stage (`UPHOLD`, `OVERRULE`, or `REOPEN`), with the
context/decision identity, effect status
(`delivered`/`pending`/`unknown`/`retired`/`reconciled`/`reconciliation-required`),
and gap identity when applicable, alongside the existing merge-gate stages.

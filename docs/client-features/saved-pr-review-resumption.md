# Saved PR review resumption

Saved PR review work is resumed through the typed, synchronous `pr_review_resumption` service before another reviewer is admitted. Both the early unresolved-thread route and the ordinary saved-result route use this boundary, which preserves closure-first ordering, the non-force 60-second unavailable-evidence deferral, force behavior, and an explicit `requires_reassessment` replay outcome.

The service is orchestration rather than a new authority. Existing review-attempt, accepted-finding, review-cycle, publication, and corrective-delivery owners remain authoritative, while `pr_processor` retains the caller-owned review runtime and every final merge gate. Its ports are bound at call time so controlled production-boundary substitutions remain available to regression tests.

The extraction is observability-neutral: it continues to emit existing processor stage events through typed ports, with the same stage identifiers, outcomes, and facts. No dashboard event schema, origin, or trace-to-view mapping changes.

When real replay of a saved NEEDS_FIX or NEEDS_TESTS result finds only resolved
or non-actionable native feedback, the ordinary caller consumes
`requires_reassessment` and continues to existing independent current-target
review admission in the same invocation. This also supports legacy published
results without a typed ordinary-correction payload. No new commit or force is
required, and replay sends no stale correction. The signal grants no closure,
PASS, approval, delivery or merge authority; current attempts, admission,
publication and final merge checks still decide the outcome. A replay without
that signal retains its existing repair, deduplication and wait behavior.

The ordinary transition emits `pr.adversarial-validation` with outcome `Deferred`
and facts `effect=saved-review-reassessment`, `requires_reassessment=true`,
`examined_head`, and `saved_verdict`. It is an admission request, not a successful
`pr.repair-delegation` delivery or a reused-review audit record. Subsequent
validation or refusal events describe the actual result; the dashboard uses its
existing stage/fact rendering with no new schema or processing origin.

Typed saved corrections also reacquire their selected canonical roots before
replay. A resolved authenticated root whose complete substantive discussion has
not been independently assessed at the saved input binding becomes an exact
`discussion_revalidation_thread_ids` obligation. The checkpoint is consumed
before Strong repair or saved ordinary replay, survives reconstruction, and
recollects the root and chronological replies even without an addressed marker.
Only those owned roots enter this local input view; native resolution is never
PASS, canonical acceptance, or permission to resend a correction.

A bound STILL_VALID disposition prevents another reviewer invocation for
unchanged evidence. New substantive replies require independent reassessment;
missing dispositions and incomplete or unauthenticated observations cannot close
the blocker. Accepted-Strong lifecycle, independent completion, publication, CI,
and merge gates retain their existing authority. Production emits
`pr.adversarial-validation` / `Deferred` with
`effect=saved-discussion-reassessment`, the exact `thread_ids`, `examined_head`,
`saved_verdict`, `requires_reassessment=true`, and a bounded reason. This records
an input obligation, not successful review or delivery.

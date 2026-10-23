# Saved PR review resumption

Saved PR review work is resumed through the typed, synchronous `pr_review_resumption` service before another reviewer is admitted. Both the early unresolved-thread route and the ordinary saved-result route use this boundary, which preserves closure-first ordering, the non-force 60-second unavailable-evidence deferral, force behavior, and an explicit `requires_reassessment` replay outcome.

The service is orchestration rather than a new authority. Existing review-attempt, accepted-finding, review-cycle, publication, and corrective-delivery owners remain authoritative, while `pr_processor` retains the caller-owned review runtime and every final merge gate. Its ports are bound at call time so controlled production-boundary substitutions remain available to regression tests.

The extraction is observability-neutral: it continues to emit existing processor stage events through typed ports, with the same stage identifiers, outcomes, and facts. No dashboard event schema, origin, or trace-to-view mapping changes.

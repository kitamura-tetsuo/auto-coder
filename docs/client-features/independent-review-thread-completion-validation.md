# Independent Review-Thread Completion Validation

  review_thread_completion_validation:
    description: "A fresh backend_adversarial_validation run also independently adjudicates any unresolved review thread an eligible automated reviewer authored and a cloud implementation agent explicitly claimed as addressed, and Auto-Coder resolves the thread only when that independent verdict, evidence, and the still-current PR head all confirm it."
    implementation: |
      ReviewThreadDisposition, VALID_REVIEW_THREAD_DISPOSITION_STATUSES,
      run_adversarial_validation(claimed_review_threads_section=...),
      parse_adversarial_validation_response (thread_dispositions parsing)
      in src/auto_coder/adversarial_validator.py,
      ClaimedReviewThread, ReviewThreadClassification, classify_review_threads,
      render_claimed_review_threads_section, resolve_addressed_review_threads,
      RESOLVER_EXPLANATION_MARKER
      in src/auto_coder/review_thread_validation.py,
      ClaimedReviewThreadGateState, _resolve_eligible_review_thread_ids,
      _get_claimed_review_thread_state
      in src/auto_coder/pr_processor.py,
      ReviewThreadComment, ReviewThread.comments, resolve_review_thread,
      reply_to_review_thread in src/auto_coder/util/gh_cache.py,
      pr.adversarial_validation ($claimed_review_threads, thread_dispositions
      output schema) in src/auto_coder/prompts.yaml
    configuration:
      file: "~/.auto-coder/config.toml or repository-scoped config.toml"
      example: |
        [github]
        pr_review_allowlist = [199175422, 123456789]
    behavior:
      - "Before the merge-gate unresolved-review-thread check skips validation, Auto-Coder fetches every review thread with its full comment list (stable numeric author identity ID, informational login, and body) via GraphQL and classifies each unresolved thread: a thread counts as 'claimed' only when its root author's stable identity ID is in the effective repository-scoped `[github].pr_review_allowlist` AND at least one reply carries the `auto-coder-review-addressed:v1` marker. Login and display-name strings never influence authorization. Missing, malformed, unconfigured, empty, or non-allowlisted identity data fails closed to an ordinary unresolved blocker."
      - "`[github].pr_review_allowlist` accepts only positive integer GitHub identity IDs; strings, booleans, zero, negative values, and other malformed values raise configuration validation errors instead of being coerced. Repository-specific config lists replace the global list through the standard partial-override precedence."
      - "When every unresolved thread is claimed (or there are none), merge processing proceeds to a fresh adversarial validation run instead of being skipped; the run's prompt includes a 'Claimed-Addressed Review Threads' section listing each claimed thread's ID, original finding (the root comment), and full chronological discussion (including the implementation agent's rationale)."
      - "Adversarial-validation response parsing supports both Codex JSONL events and Claude CLI stream-json events. Claude streams must begin with system/init and contain exactly one successful terminal result; malformed, truncated, or failed streams fail closed without treating event metadata as the validator verdict."
      - "Every PR-processing entry point uses the same claimed-aware merge transition workflow. A quick/single-PR pass cannot bypass independent validation or re-delegate claimed-addressed threads, and when claimed and ordinary unresolved threads coexist only the ordinary blockers are included in repair delegation."
      - "The validator returns one independent disposition per claimed thread in a `thread_dispositions` list: `ADDRESSED` (the original defect is demonstrably gone on the current head), `STILL_VALID` (it remains), or `INCONCLUSIVE` (the evidence cannot establish either safely). The prompt explicitly forbids deriving ADDRESSED merely from the implementation agent's claim, a GitHub `outdated` state, a moved/deleted diff line, a passing test alone, or a pushed commit alone; every disposition requires a concrete rationale and evidence grounded in the current implementation."
      - "Thread dispositions are parsed leniently and independently of the PR-level verdict: a malformed or incomplete entry is dropped (logged) without failing the PR-level PASS/NEEDS_FIX/INCONCLUSIVE/BLOCKED result, and a thread may be ADDRESSED while the PR itself is NEEDS_FIX for an unrelated defect, or vice versa."
      - "Resolution is fail-closed end to end (`resolve_addressed_review_threads`): it acts only on dispositions whose status is exactly ADDRESSED, ignores an ADDRESSED disposition for any thread ID that was not among this run's claimed threads, re-fetches the PR's current head SHA and refuses to resolve anything if it no longer equals the validated head, then for each remaining thread first posts a resolver explanation reply (marked with `RESOLVER_EXPLANATION_MARKER`, distinguishable from the implementation agent's own addressed claim) and only calls the `resolveReviewThread` GraphQL mutation if that reply succeeded; a failed reply, a failed or unconfirmed resolve mutation, or a head-verification failure leaves that thread unresolved without affecting any other thread's resolution."
      - "Stale-resolution BLOCKER/CLEARED comments are authoritative only when their author matches the login proven by Auto-Coder's active GitHub credential; exact lookalike markers from humans or implementation agents are ignored. A resolved thread with a truncated comment list has incomplete latest-marker evidence regardless of which marker is visible, so merge processing fails closed without attempting an automatic unresolve from that partial page."
      - "Every resolution-time head comparison and stale-marker head comparison uses a strict direct REST lookup that bypasses the shared GitHub GET cache. A stale-marker scan that performs a head comparison repeats that authoritative lookup after scanning all threads and requires the same head SHA, so a push from H1 to H2 during the scan fails closed without mutation. A missing, malformed, failed, or changed authoritative lookup cannot validate a resolution or dismiss an H1 blocker."
      - "Before attempting a stale-resolution rollback, Auto-Coder durably records a fail-closed rollback-transition state. A later run reconciles that transition only when the strict GitHub lookup returns the exact thread once with an explicit boolean resolved state: an unresolved thread advances to marker cleanup without another unresolve, while a still-resolved thread retries rollback under the existing durable guard; missing, duplicate, malformed, or failed lookup evidence remains blocking. After confirmed rollback, a failed CLEARED-marker reply becomes marker-cleanup-only state, and if both that reply and cleanup-state persistence fail, the pre-mutation transition remains available for this reconciliation. Resolve and unresolve mutations are accepted only when GitHub returns the exact requested thread ID and the expected literal resolved state."
      - "`_merge_pr`'s existing pre-merge safeguard re-fetches the authoritative GitHub review-thread state after any resolution and before merging, so automatic merge proceeds only when no unresolved thread remains (claimed-but-unresolved threads included) and every other merge gate also passes."

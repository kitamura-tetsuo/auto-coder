# PR Review Thread Merge Gate

  unresolved_review_threads_gate:
    description: "Block PR auto-merge while unresolved review threads exist."
    implementation: |
      ReviewThread, get_pr_review_threads, has_unresolved_review_threads in src/auto_coder/util/gh_cache.py,
      has_unresolved_review_threads, _process_pr_for_merge, _handle_pr_merge, _merge_pr in src/auto_coder/pr_processor.py
    behavior:
      - "Before performing an automatic merge, retrieves the PR review threads from GitHub GraphQL API (PullRequestReviewThread.isResolved)."
      - "If any review thread is unresolved (isResolved == False):"
        - "The PR is not merged and remains open."
        - "When the PR is associated with an existing Codex Cloud task, Auto-Coder assigns the unresolved review work to that same task through a follow-up. The prompt pins work to the current PR head/base branches, requires fixes and tests to be pushed to the existing PR, and forbids replacement branches or PRs."
        - "Cloud repair delivery keeps a stable per-finding identity for cross-path deduplication and a durable remediation-generation identity derived from terminal activity of the owning implementation task or applicable implementer provenance evidence. For Codex Cloud, each accepted adversarial follow-up durably records its own pre-send assistant-turn baseline; immediately before a new validation, the first distinct provider-observed completed assistant turn whose timestamp orders it after that baseline is durably bound to that finding's generation. The validation attempt's complete per-finding observation snapshot is persisted before publication, including absent generations, so a later routing lookup failure or saved-report replay cannot retroactively use newer activity and a multi-finding report cannot substitute one finding's baseline for another. Reconciliation of an earlier provider delivery records only the finding and generation actually recovered; it cannot satisfy a later validation or completed-turn delivery obligation. Repeated observations of the selected turn, later unrelated turns, nonterminal turns, provenance replies, status or head changes, and provider history that cannot be chronologically ordered after the baseline do not advance it. The first independent failed revalidation after genuine corrective activity is delivered once to every supported originating provider with an explicit failed-correction explanation. Both identities are recorded locally and in trusted PR-side receipts, so this lifecycle distinction survives restarts while ordinary unresolved-thread routing remains deduplicated. WHAM 429/5xx and transport failures retain their reconciliation safeguards, and definite client rejection remains retryable."
        - "Linked issue attempt counters are not incremented."
        - "The condition is treated as deferred processing rather than an execution failure."
        - "Future runs re-check the review threads and merge once all threads are resolved."
      - "Resolved review threads (isResolved == True) and normal PR conversation comments do not block merge."
      - "Applies generically to all PRs handled by Auto-Coder."

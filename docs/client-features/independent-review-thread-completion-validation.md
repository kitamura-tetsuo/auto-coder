# Independent Review-Thread Completion Validation

A saved current-head PASS does not make an unresolved authenticated adversarial
finding newly actionable implementation feedback. Such a remaining thread is
admitted for independent per-thread closure, even without an implementer reply
and even at the normal review-count limit. The saved PASS is not reused as a
substitute for that thread's disposition. Initial admission and the post-Codex
recheck retain the same closure input. The prompt identifies this as unfinished
closure after PASS, without claiming a head change or an explicit `--force`.
Its heading and discussion instructions live in `prompts.yaml` and use
`render_prompt(..., include_header=False)` because they are fragments within
the complete validator prompt. Ordinary prompt rendering still includes its
global header by default; label-selected fragments preserve this option.
Unauthenticated, truncated, and unrelated human-review blockers retain their
ordinary blocking/repair behavior. No thread is resolved from PASS alone.

Production emits `pr.review-thread-gate` with `DEFERRED` and reason
`saved PASS requires independent thread closure`; it emits no corrective
handoff for those remaining authenticated findings before independent validation.
The production regression is
`test_unresolved_adversarial_finding_revalidates_new_head_or_saved_pass` in
`tests/test_adversarial_validation_pr_flow.py`.

## Reusable closure status reply

Each independent `ADDRESSED` decision carries a deterministic binding to the
complete native-thread discussion supplied to validation. Auto-Coder performs
an authoritative read before canonical acceptance and again immediately before
resolution; an added, edited, removed, incomplete, or ambiguous substantive
comment leaves the thread unresolved for a later independent evaluation. The
one authenticated, schema-valid controller closure receipt is housekeeping and
is excluded from this binding, while forged or marker-looking ordinary replies
remain substantive evidence.

Independently accepted closure maintains one controller-owned reply per exact
API origin, repository, PR, thread and root comment. The reply begins with
`<!-- auto-coder-review-thread-closure:v1 -->` and contains a versioned metadata
envelope, the full validated head, its closure state, and the validator's
rationale and evidence. Repeated processing, reconstructed clients and
publication-failure reopening update this reply instead of appending another
explanation, resolve-intent marker and cleared marker. Historical replies remain
unchanged.

The operation key binds the accepted head, supplied base and requirement
revision, root finding, canonical blocker ownership, correction scope and
concern IDs. Retry-attempt IDs, ledger revisions and generated explanation
wording do not change the reply identity. The receipt records external effects;
it never supplies an `ADDRESSED` disposition or substitutes for current
independent adjudication, canonical persistence or compound-root gating. New
substantive review input and changed head or scope continue through independent
validation before any new closure effect.

| Receipt state | Meaning |
| --- | --- |
| `PENDING` | GitHub durably exposes intent for this operation before resolution. The resulting thread state or cleanup may still need reconciliation. |
| `RESOLVED` | Resolution was confirmed for the validated head and the same reply was updated to record completion. |
| `ROLLBACK_REQUIRED` | Review publication failed after closure. Reopening remains required for this exact operation, including at the same head and after local registry loss. |
| `ROLLED_BACK` | Rollback of the bound operation was confirmed; this does not authorize a later resolution. |

Every actual resolve, including a second resolve after review-publication
failure, first re-arms the same reply as `PENDING`. An earlier completed reply
cannot protect a new resolve. A complete strict observation of a matching
already-resolved operation allows its confirmed effect to be reused after fresh
independent acceptance; it does not cause another reply or resolve mutation.

Receipt lookup authenticates the author through the active GitHub credential,
requires the exact thread and root once, and reads all comment pages. Foreign
lookalikes cannot be adopted. Missing target threads, truncated observations,
duplicate, malformed or unsupported owned receipts, failed reads and unconfirmed writes
produce explicit unfinished outcomes. Only a complete valid observation with
no owned receipt permits first publication. POST and update effects are read
back; an accepted write whose response was lost is reconciled from GitHub state
instead of causing a blind replacement reply.

Resolution, stale recovery and publication-failure rollback use the same
bounded PR lock from `runtime_locks.file_lock`. Ordinary PR processing holds
this lock across closure effects, reviewer publication and any required
publication-failure rollback as one interval, so startup recovery cannot
interleave before that rollback finishes. Its key includes normalized API
origin, case-normalized repository and PR number under the
`review-thread-closures` runtime namespace. The root is
`AUTO_CODER_RUNTIME_ROOT`, or `~/.auto-coder/runtime` by default. Contention
defers the invocation; a later invocation acquires the lock and observes the
winner's current receipt before publishing. This coordinates participants
sharing that runtime namespace.

## Recovery and legacy histories

The strict pre/post head checks, bounded rollback attempts and durable
`StaleReviewThreadRegistry` remain in the execution path. A pending receipt is
also GitHub-side recovery evidence when local persistence fails. Cleanup after
confirmed rollback records the exact receipt operation key, so a later retry
can finish that historical operation without reopening a newer legitimate
resolution or overwriting a different operation's receipt. Missing provenance
does not authorize clearing an unrelated pending operation.

After review-publication failure, the controller first updates the existing
authenticated reply to `ROLLBACK_REQUIRED`, preserving its exact operation and
head, before recording the local transition and attempting to reopen the
thread. This state survives simultaneous local-registry write failure and
failed unresolve attempts. Reconstructed processing discovers it even at the
same head and blocks further closure or merge until rollback is confirmed. It
cannot be promoted back to `RESOLVED` or rebound to a new closure. An already
unresolved thread needs receipt cleanup only; recovery does not send another
unresolve. If the receipt update fails, a successfully stored local transition
still protects rollback. Both recovery paths use the same bounded PR lock.

Mutable receipts are interpreted independently of chronological legacy
`BLOCKER`/`CLEARED` events: editing a comment does not move it to the end of the
thread. Legacy head-bound and headless markers remain conservatively readable.
A completed receipt does not cancel an active legacy blocker. After confirmed
rollback, an observed active legacy event is closed using its existing
`CLEARED` marker format; a subsequent complete history already containing that event's
clear does not cause another housekeeping reply.

A confirmed GitHub resolve with an unconfirmed receipt-completion update keeps
`effect_state=CONFIRMED` but reports a `marker-cleanup` warning and an unfinished
closure. The existing `pr.review-thread-closure` BLOCKED event exposes the exact
phase and reason, and PR processing does not merge that invocation. This keeps
the semantic verdict, native thread effect and outstanding cleanup distinct.
Controlled transport regressions are in
`tests/test_review_thread_closure_receipt.py`; the real-resolver publication
failure/retry integration is
`test_real_thread_closure_reuses_reply_after_pr_publication_failure` in
`tests/test_adversarial_validation_pr_flow.py`.

  review_thread_completion_validation:
    description: "A fresh backend_adversarial_validation run also independently adjudicates any unresolved review thread an eligible automated reviewer authored and a cloud implementation agent explicitly claimed as addressed, and Auto-Coder resolves the thread only when that independent verdict, evidence, and the still-current PR head all confirm it."
    implementation: |
      ReviewThreadDisposition, VALID_REVIEW_THREAD_DISPOSITION_STATUSES,
      run_adversarial_validation(claimed_review_threads_section=...),
      parse_adversarial_validation_response (thread_dispositions parsing)
      in src/auto_coder/adversarial_validator.py,
      ClaimedReviewThread, ReviewThreadClassification, classify_review_threads,
      render_claimed_review_threads_section, resolve_addressed_review_threads,
      StaleReviewThreadRegistry, retry_pending_stale_review_thread_rollbacks
      in src/auto_coder/review_thread_validation.py,
      execute_durable_thread_closures in src/auto_coder/pr_blocker_closure.py,
      ClosureReceipt, observe_closure_receipt, put_receipt, closure_receipt_lock
      in src/auto_coder/review_thread_closure_receipt.py,
      ClaimedReviewThreadGateState, _resolve_eligible_review_thread_ids,
      _get_claimed_review_thread_state
      in src/auto_coder/pr_processor.py,
      ReviewThreadComment, ReviewThread.comments, resolve_review_thread,
      reply_to_review_thread, update_review_thread_comment
      in src/auto_coder/util/gh_cache.py,
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
      - "The controller binds each produced disposition to the complete substantive discussion supplied to that validation and retains that binding with ordinary-result recovery. Closure re-observes the native thread before canonical acceptance and immediately before resolution; any added, edited, removed, incomplete, or ambiguous ordinary discussion revokes stale acceptance and requires independent reevaluation. Only a schema-valid authenticated owned closure receipt or strict two-tier closure-status envelope is excluded from the binding; marker-looking, malformed, duplicated, or unauthenticated comments remain substantive."
      - "Resolution is fail-closed end to end (`resolve_addressed_review_threads`): it acts only on independently accepted ADDRESSED dispositions for threads actually claimed in this run, persists canonical acceptance and enforces all owned obligations before effects, and refuses stale heads. Each actual resolve requires the authenticated reusable reply to be observably PENDING for its exact operation. Missing, ambiguous or failed receipt evidence and unconfirmed resolve effects produce unfinished outcomes without blocking successful sibling closures or fabricating resolution."
      - "Stale-resolution BLOCKER/CLEARED comments are authoritative only when their author matches the login proven by Auto-Coder's active GitHub credential; exact lookalike markers from humans or implementation agents are ignored. A resolved thread with a truncated comment list has incomplete latest-marker evidence regardless of which marker is visible, so merge processing fails closed without attempting an automatic unresolve from that partial page."
      - "Every resolution-time head comparison and stale-marker head comparison uses a strict direct REST lookup that bypasses the shared GitHub GET cache. A stale-marker scan that performs a head comparison repeats that authoritative lookup after scanning all threads and requires the same head SHA, so a push from H1 to H2 during the scan fails closed without mutation. A missing, malformed, failed, or changed authoritative lookup cannot validate a resolution or dismiss an H1 blocker."
      - "Before attempting a stale-resolution rollback, Auto-Coder durably records a fail-closed rollback-transition state. A later run reconciles that transition only when the strict GitHub lookup returns the exact thread once with an explicit boolean resolved state: an unresolved thread advances to cleanup without another unresolve, while a still-resolved thread retries rollback under the existing durable guard; missing, duplicate, malformed, or failed lookup evidence remains blocking. After confirmed rollback, a failed receipt update or required legacy CLEARED write becomes cleanup-only state bound to the historical receipt operation; if cleanup-state persistence also fails, the pre-mutation transition remains available. Resolve and unresolve mutations require the exact requested thread ID and literal expected resolved state."
      - "`_merge_pr`'s existing pre-merge safeguard re-fetches the authoritative GitHub review-thread state after any resolution and before merging, so automatic merge proceeds only when no unresolved thread remains (claimed-but-unresolved threads included) and every other merge gate also passes."
      - "Closure execution returns one typed outcome for every claimed thread selected in the invocation, including omitted, rejected, non-authorizing, failed, unconfirmed, and not-attempted work. Each outcome keeps the independent decision, durable acceptance, and exact GitHub effect state separate and carries the repository, PR, evaluated head, attempt, root, and known blocker identities."
      - "PR processing emits confirmed and unfinished counts plus each unfinished thread ID, phase, and reason on `pr.review-thread-closure`. A semantic PASS remains a semantic PASS, but an unfinished closure produces a failed processing outcome and prevents merge without being delegated as an implementation defect; a later independently admitted successful attempt can progress normally."

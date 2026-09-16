# PR adversarial validation backend exhaustion (EXHAUSTED)

`EXHAUSTED` is a supported PR adversarial-validation result, published through the
same result path as `PASS`/`NEEDS_FIX`/`NEEDS_TESTS`/`BLOCKED`/`INCONCLUSIVE`/`ERROR`.
It is produced only when every otherwise-valid candidate on the authoritative PR
backend route (a present PR-specific order/single-backend configuration, else the
legacy `[backend_adversarial_validation]` order/single-backend configuration, else
the high-score fallback; disabled candidates and candidates whose resolved effective
backend type is not `claude`/`codex`/`muse` are excluded from that route entirely) is
confirmed quota/usage-capacity exhausted by a successful current quota observation.
Missing/failed usage retrieval ("quota-unknown") and any non-quota unavailability
(missing prerequisites, auth failure, backend-manager construction failure,
execution failure) never produce `EXHAUSTED`; a candidate in either state keeps the
route runnable or falls back to the existing generic `BLOCKED` outcome.

`EXHAUSTED` is fail-closed like every other non-`PASS` result: it never authorizes
automatic merge and is never treated as an actionable corrective verdict (it does not
send `NEEDS_FIX`/`NEEDS_TESTS`-style remediation feedback).

Unlike `BLOCKED`/`ERROR`/`INCONCLUSIVE` (which never self-retry for an unchanged PR
HEAD), a currently applicable published `EXHAUSTED` result establishes a deferred
retry obligation for that exact HEAD: the published comment/review durably records a
retry-not-before time (the earliest authoritative quota reset time when every
exhausted candidate reported one, else a finite cooldown). Once that time is reached,
ordinary Auto-Coder processing automatically starts a new adversarial-validation
attempt for the unchanged HEAD without a new commit, manual comment/review activity,
or `--force`, bypassing both the same-HEAD deduplication gate and the maximum
adversarial-review-count gate. The retry reevaluates current HEAD, candidate route,
and quota evidence from scratch; it is bound to the exact HEAD SHA and the
originating attempt sequence via the existing durable attempt-sequence store, so a
newer attempt for the same HEAD (a force/provenance-triggered revalidation, or
another due retry) supersedes an older pending retry, and a HEAD change invalidates
it outright.

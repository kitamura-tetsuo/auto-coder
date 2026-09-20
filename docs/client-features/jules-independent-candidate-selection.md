# Independent Jules candidate selection

Speculative Jules artifacts are accepted only from a current, complete evidence
bundle for the exact candidate PR revision. Acceptance requires verified,
nonconflicting provenance; an open, non-draft PR with a nonempty effective diff;
complete exact-head CI observations containing positive success evidence; all
additional merge guards; and an enabled independent adversarial validator whose
normalized `PASS` covers the captured Requirements fingerprint without blocking
findings or specification gaps.

Unavailable, partial, pending, stale, contradictory, all-neutral, and all-skipped
evidence remains pending. Only a definitive empty diff, completed CI failure, or
valid `NEEDS_FIX` with a merge-blocking finding is classified for candidate-only
retirement. This boundary does not repair a candidate, mutate its source Issue,
or advance the source attempt.

Every accepted record binds the repository, generation and candidate, PR identity,
head and observed base SHAs, captured Requirements fingerprint, candidate-binding
revision, validation revision, and invalidation revision. A newer invalidation,
head, base, oracle, or binding makes the earlier evidence unusable. The competition
ledger's serialized selection transition chooses the first current record that
commits; it never waits for or ranks another candidate. That transition keeps the
selected pair immutable and retires its siblings.

Selection is merge admission rather than implementation completion. The final PR
merge sender re-reads speculative authority immediately before creating its durable
merge operation. An active unselected, retired, conflicting, or otherwise uncertain
artifact cannot reach the outbound mutation; legacy PRs continue through the
ordinary gates. The merge API still receives its existing exact-head guard, and
only authoritative confirmed merge completion may trigger the existing success
and linked-Issue completion behavior.

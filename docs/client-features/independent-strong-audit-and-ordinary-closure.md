# Independent strong audit and ordinary closure execution

`pr_review_execution.py` provides the read-only reviewer boundary consumed by
the durable two-tier PR review cycle. `STRONG_AUDIT` and `ORDINARY_CLOSURE` are
explicit roles even if configuration maps them to the same provider and model.
The strong role uses only `backend_strong_pr_adversarial_validation`; the
ordinary role retains the dedicated PR, general adversarial, then high-score
precedence. An absent, unusable, or exhausted strong route never falls through
to an ordinary route.

Every invocation is self-contained and bound to the requested head, reviewed
base, Requirements snapshot, strong-policy identity, round, and attempt. The
execution checkout's HEAD is checked before and after the no-edit call. Strong
audits start without prior verdicts, finding dispositions, or provider session
memory. Ordinary closure instead receives the immutable portable finding bundle,
its revision, current source and tests, and the complete audited-head-to-current-
head corrective diff.

Strong findings preserve their stable ID, exact Requirement references and
text, reachable counterexample, expected and actual behavior, evidence,
production boundary, consequence, and focused regression oracle. A regression
gap also records the incorrect implementation admitted by existing tests and why
those tests remain green. Ordinary closure must return exactly one evidence-backed
`FIXED`, `INVALID`, `OPEN`, or `INCONCLUSIVE` disposition for every accepted
finding and separately classify cumulative scope as `BOUNDED`, `EXPANDED`, or
`UNKNOWN`. It may report new concrete findings. Ordinary `PASS` with `EXPANDED`
or semantically `UNKNOWN` scope remains a complete convergence result, but it
cannot produce closure evidence; production durably admits a renewed independent
strong audit. Only complete dispositions, no new findings, and evidence-backed
`BOUNDED` scope can produce closure evidence.

The output parser validates every execution identity and required field. Invalid,
stale, incomplete, contradictory, or unavailable output becomes an explicit
non-complete diagnostic, never PASS. The result is evidence for the durable
lifecycle; it cannot publish reviews, close threads, mutate Issues, or merge.
The caller's `ReviewExecutionInput.mode` selects the response schema and supplies
the result's role. Model output need not echo `mode`; any returned mode field has
no authority to select a different schema or bypass closure requirements.
`tests/test_pr_review_execution.py` covers responses without a mode and attempts
to override the caller's role. Trace stages, routing, and durable result schemas
are unchanged: they continue to use the caller-owned role.

Production PR processing consumes `STRONG_PENDING` after an applicable ordinary
PASS. It atomically claims the durable phase, creates a detached read-only
worktree at the audited head, resolves only the configured strong route, supplies
the complete base-to-head diff and Requirements contract, and durably accepts
the validated portable result. Unavailable, exhausted, inconclusive, failed,
and contended executions remain pending with diagnostic and retry evidence;
accepted results remain blocked on their separately owned publication or repair
effect and therefore do not grant merge authority.

When accepted strong findings survive into a later ordinary-pass head,
production processing instead runs `ORDINARY_CLOSURE` through the ordinary PR
route. The invocation receives the retained finding payloads and revision, the
fresh Requirements snapshot and repository paths, and the cumulative diff from
the strong-audited head to the current head. Acceptance uses the pre-invocation
durable transition version as a fence. Bounded convergence records a pending
closure certification; expanded or semantically unknown convergence records the
non-closing assessment and requires a renewed strong round. Trace events use the
`pr.ordinary-closure` stage and expose the head/base, backend, contract and policy
identities, finding revision and IDs, phase, and acceptance/defer reason.

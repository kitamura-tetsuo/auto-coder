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
non-complete diagnostic, never PASS. Before schema validation, normalization
removes only recognized Claude CLI transport wrappers and Markdown presentation
wrappers without synthesizing review content. A Claude `stream-json` capture must
contain a `system/init` event and exactly one terminal successful `result` event
as its last line, while a `--output-format json` capture must be a single
successful `result` envelope; the envelope's result string is the sole
authoritative answer and transport failures never fall back to intermediate
messages or transcript fragments. The authoritative answer must contain exactly
one complete review object, optionally in one `json`-tagged fenced block, with no
competing candidates, duplicate members, top-level arrays, or repaired syntax.
Diagnostics distinguish transport, answer-JSON, and schema/identity failures with
concrete reasons, keep the original capture in the interaction log, and bound any
redacted preview to 2,000 characters. The result is evidence for the durable
lifecycle; it cannot publish reviews, close threads, mutate Issues, or merge.
The prompt specifies the exact portable finding keys and types, including
string evidence, paired Requirement ID/text arrays, and the additional evidence
required for regression gaps. Prompt-schema regression tests pass the rendered
example directly to the production parser without translating model output.
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

Availability resolution applies the loaded `quota_selection.strategy` at its
initial candidate-classification boundary, consistently for issue, ordinary PR,
and strong PR routes. Consequently, `burst` keeps a Codex reviewer runnable while
weekly quota remains positive even below the surplus reserve, whereas zero quota
under `burst` and below-reserve quota under `surplus` remain exhausted. Unknown
quota and non-quota construction failures retain their existing unavailable,
non-exhausted behavior, and a strong route never borrows an ordinary fallback.

When accepted strong findings survive into a later head, production processing
does not run a separate closure-only reviewer. Instead, before the one ordinary
validation of that head is invoked, `_capture_ordinary_closure_input()` captures
the authoritative closure context from the durable review cycle (never from the
GitHub thread or reviewer-session view): repository, PR, open epoch, the
registered ordinary attempt ID and sequence, H2/B/M/P, the accepted strong
round and audited H0, the complete outstanding finding bundle and its revision,
and the cumulative H0-to-H2 diff (an empty diff is reported as observed
evidence, so a same-head correction or rebuttal needs no artificial new head).
That context rides with the same ordinary invocation that performs the normal
Requirements/code/test review, which returns the ordinary verdict and a
`closure_assessment` together. The retained semantic result is the ordinary
review with the accepted findings under closure settled, so independent
blockers still count and closure is reachable before any effective PASS.

`_apply_ordinary_closure_evidence()` then durably retains that result through
`OrdinaryClosureEvidence` (retention precedes and survives any observation
failure) and applies it to the owning cycle against a freshly observed H/B/M/P,
the attempt fence, and the strict head read. No model is called after the
ordinary result. A missing or malformed assessment, an `OPEN`/omitted/
`INCONCLUSIVE` disposition, or an independent ordinary blocker certifies nothing
and is reported as unavailable or retained; it never triggers an immediate
closure-only fallback. A later normally admitted retry for genuinely incomplete
validation uses the same combined path, and a complete assessment spends the
single closure-aware review of its corrective generation (the existing
`closure_attempts` record), so repeated scheduling cannot repeat it.

Before any same-head cache or ordinary-PASS shortcut and before a reviewer is
admitted (`_resume_closure_before_admission()`), retained sources are
reconciled without a model: outstanding certification, bookkeeping and
publication resume; an accepted retained semantic PASS at the current head is
reused rather than re-reviewed; a legacy ordinary PASS with closure still
outstanding and no retained assessment triggers one combined closure-aware
revalidation; and an unreadable target or store defers with the evidence
retained (a normal retry resumes it; `--force` may still start a fresh attempt,
which carries the same closure context). Bounded convergence records a pending
closure certification; expanded or semantically unknown convergence records the
non-closing assessment and requires a renewed strong round.

The closure publication is derived deterministically from the accepted ordinary
source and names its real ordinary attempt, evaluated head and actual reviewer
and states that it consumed an existing attempt without an additional model
execution. An unconfirmed publication resumes through the existing effect owner
and its acknowledgement; semantic certification alone is never merge authority.
Trace events use the `pr.ordinary-closure` stage and expose the head, backend,
`evidence_status`, `source_attempt_id`, `source_attempt_sequence`,
`additional_model_execution` (always `false`), `renewed_strong_required`, the
publication result, and the acceptance/defer reason.

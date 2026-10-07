# Dashboard LLM review history

An explicit retry of failed scoped local verification appears in the existing
repair-delegation stage with `explicit_retry_requested`; the published report
identifies its attempt number. Earlier failed reports remain history. A retry
request itself does not establish correction or merge completion.

Ordinary reviews with concrete requirement violations show correction handoff
through the existing repair-delegation stage. A matching `VIOLATED` entry does
not mean evidence is missing; unmatched or unverified entries still show a
reconciliation wait. Repair dispatch, publication, independent verification,
and confirmed merge remain separate history facts.

When a repair review encounters stale base/contract/policy authority, the
Execution Trace records the renewed strong audit's current head/base and
publication result. Audit completion alone does not close historical findings;
their independent closure and the confirmed merge remain separate events.
Review-thread closure recognizes concrete browser spec citations as regression
evidence even when the reviewer does not use the words `test` or `oracle`.
Accepted citations follow the existing confirmed-closure history; production-only
paths and green CI alone still leave the independent-decision outcome unfinished.
An unavailable renewed reviewer is shown as deferred in the same stage timeline.

Automatic local rereview without a retained private workspace records a fresh
provider invocation while carrying durable finding obligations forward. A stored
session ID alone never makes the history a resumed interaction. Explicit
continuation failures remain failures, with no fallback success recorded.

Muse no-edit reviews permit shell inspection by default. Their existing audit
entries still record the actual provider result: shell availability is not
verification completion, and a result rejected by the repository snapshot or
Git-command mutation audit remains a failed interaction. No history schema,
polling behavior, or dashboard authorization changes accompany this capability.
The runnable production-boundary coverage is documented under Muse execution
in `docs/dashboard-observability.md`.

The dashboard exposes the mounted repository's durable local review audit at
`/dashboard/reviews` and from the **Review History** section of Issue and PR
detail pages. This history is independent of open-item, queue, worker, and
process-local trace retention, so a completed or closed target remains
discoverable after restart when its audit database is retained.

History pages use creation-sequence snapshot pagination (50 records by
default, bounded to 200) and apply exact target and review-kind filters in the
audit query before the page limit. A selected record displays the captured
generation and policy identity, native report, backend interactions, requested
and separately reported model, reuse provenance, and recorded effects. Parent
decomposition records are related to a child only through captured membership;
they remain parent-set reviews and link to their captured parent target.
The repository first page follows newly retained records; navigating to an
older page pins that page's high-water snapshot. Target detail history reads
the latest 500 records and explicitly labels the view when older records are
omitted by that bound.

Review history is historical evidence, not authorization or live state.
`EXECUTED`, `REUSED`, `LOCAL_ONLY`, and `BYPASSED` retain their distinct
meanings; a native PASS or READY is not merge, publication, or implementation
completion. Missing audit, report, trace, model, or legacy reuse provenance is
shown as unavailable rather than inferred. The Execution Trace remains a
separate, process-local panel.

If a provider returns but result handoff or session persistence fails, its
interaction remains `RAISED` in review history. A further failure while recording
session cleanup does not replace the original operational error or turn the
interaction into a completed result. Invocation checkpoint protection remains
process-local and does not certify publication or recovery in this view.

For explicit-local PRs, the Execution Trace's existing repair-delegation stage
also covers adversarial failure and saved-report replay. It displays
`route_disposition=LOCAL_EXECUTION` and `local_phase` for a pending local
correction, with a deferred outcome until independent validation. Routing
refusal is failed; local publication alone does not certify a successful review
or merge.

Codex execution-safety failures retain a bounded, redacted executor diagnostic
in the interaction error. The existing strong-audit Execution Trace reason and
durable pending reason carry that diagnostic too, so missing worker credentials
or failed cgroup confirmation remain visible while the review stays pending.
These diagnostics do not certify review completion or authorize provider retry.

Complete nonbounded finding convergence from a retained INCONCLUSIVE closure
assessment appears in the existing ordinary-closure trace as accepted evidence
with `renewed_strong_required=true` and `additional_model_execution=false`.
The original review verdict remains in history; this event confirms lifecycle
convergence, while Strong audit and merge confirmation remain separate gates.

Strong-audit preparation failures also appear in the existing Execution Trace
reason: exact-base fetch/verification, reviewed-diff, and tracked-path failures
include Git exit codes and bounded redacted diagnostics. They occur before a
provider invocation, so no completed review-history interaction is implied.
Missing base commits are fetched by their pinned SHA before evidence collection;
failed preparation retains the pending audit without granting merge authority.

The views poll the local audit once per second. They retain the last successful
display as stale when a read fails, preserve a selected review by `review_id`,
and avoid rebuilding unchanged report/list content. Dashboard actions only
read the mounted repository's audit root and update client state: they do not
query GitHub/providers, dispatch or retry reviews, mutate authorization/audit
state, or accept repository/file paths from the request. Report content is
rendered as inert text.

Ordinary adversarial reviews retain every semantic finding in the native report,
even when equivalent observations share a single published root. Attached-thread
counts describe GitHub publication, not the number of findings displayed here;
publication confirmation still appears as a separate effect. Blocker identity
matching and same-batch root consolidation do not change this view's audit schema,
polling, or authorization boundaries.

Historical two-tier root parsing preserves each distinct correction's retained
owner during publication reconciliation. This prevents a false ambiguity from
blocking publication while keeping the native report and publication effect
separate in this view. Contradictory explicit identities remain a publication
failure in the existing validation trace; parsing success alone does not confirm
publication.

Scope ambiguity now continues through a conservative split that preserves all
existing owners. After durable split admission, `pr.adversarial-validation` emits
`phase=reconciliation-split`, `outcome=completed`, and a reason identifying the
split and historical root or considered owners. This event confirms only the
ledger split. Native publication still has its separate confirmed/failed audit
effect, and the subsequent validation verdict still controls repair and merge.
The existing detail trace displays the split without changing the native report.

Recovery of missing GitHub review-list anchors uses an individually verified
comment receipt before confirming publication. It preserves the same separate
publication effect and historical native report; an unavailable or mismatched
receipt remains incomplete rather than appearing as confirmed publication.

Pending publication recovery before validation appears in the existing
`pr.adversarial-validation` trace stage with `phase=publication-recovery` and a
failed outcome when recovery cannot confirm the receipt. The reason is retained
in processing status and the trace. Confirming original anchors after a later
commit allows normal validation and repair routing to resume; it does not grant
merge approval from an older verdict.

Saved non-pass review replay recognizes exact `STILL_VALID` thread dispositions,
including strong-audit roots, without requiring repeated finding prose. The
existing repair-delegation stage reflects local admission failure as `FAILED`
and pending correction as `DEFERRED`. The worker diagnostic also exposes the
deferred outcome and its actions through the existing generic trace details.
Scoped local verification includes exact human-review roots already owned by a
pending repair generation, while preserving their ordinary GitHub merge gate.
Empty-root failures recorded before reviewer entry can recover on the same head;
the existing repair-delegation event shows the resulting dispositions and pending
reasons. The explicit-target Reason and action list expose those reasons as well.

Accepted-finding adjudication of a shared historical review root uses Strong
source aliases, and historical roots with the declared oracle-gap fields retain
the test-oracle category. The dashboard continues to display the existing
effective `review_disposition` and publication outcomes; an ambiguous accepted
source remains reconciliation, while an accepted current closure can proceed to
verification. There are no new history fields or inferred completion states.

An unresolved authenticated adversarial thread after a saved current-head PASS
appears as deferred independent closure in the existing review-thread gate stage,
with its examined head and reason. It does not appear as a newly accepted repair
handoff. The review-history PASS and confirmed GitHub thread-resolution effects
remain separate evidence; neither is inferred from the other.

Strong findings awaiting correction appear through the existing
`pr.repair-delegation` stage with `effect="strong-findings-before-validation"`,
the examined head, canonical blocker IDs, handoff and wait reason. Confirmed
handoff is `ACCEPTED_HANDOFF`; unavailable publication, identity or repair route
is `DEFERRED`. `ordinary_reviewer_admitted=false` indicates that no ordinary
reviewer was admitted to rediscover the unchanged Strong findings. Correction
completion subsequently uses the existing ordinary validation/closure history.

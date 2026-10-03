# Dashboard LLM review history

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

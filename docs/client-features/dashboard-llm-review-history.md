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

Review history is historical evidence, not authorization or live state.
`EXECUTED`, `REUSED`, `LOCAL_ONLY`, and `BYPASSED` retain their distinct
meanings; a native PASS or READY is not merge, publication, or implementation
completion. Missing audit, report, trace, model, or legacy reuse provenance is
shown as unavailable rather than inferred. The Execution Trace remains a
separate, process-local panel.

The views poll the local audit once per second. They retain the last successful
display as stale when a read fails, preserve a selected review by `review_id`,
and avoid rebuilding unchanged report/list content. Dashboard actions only
read the mounted repository's audit root and update client state: they do not
query GitHub/providers, dispatch or retry reviews, mutate authorization/audit
state, or accept repository/file paths from the request. Report content is
rendered as inert text.


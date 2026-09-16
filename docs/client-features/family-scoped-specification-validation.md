# Family-scoped specification validation

Before standalone, child, or decomposition validation is admitted, Auto-Coder
uses lightweight Issue-body discovery, reusing the existing one-hour open-Issue
list cache when available, to discover supported
`Parent-Issue` declarations belonging to the affected family. Only those
candidate children and native direct children use validated HTTP snapshots;
unrelated Issues are not individually refreshed. Fresh HTTP cache entries are
reused across preflight and subsequent family checks. Each resource/page is sent
once while fresh; expired responses use normal HTTP revalidation. Unchanged
parent declarations and the final family assembly avoid redundant readback. A body-only child absent from cached discovery and native membership
is discovered after list refresh; the discovery list alone never authorizes linking or
implementation. Issue snapshots, parent identity, child membership, and dependency
relations use the shared private HTTP cache with strict payload/error handling.
Local relationship, label, or state writes invalidate prior read evidence, including
cached absence and every dependency/list page, before readback. One pre-dispatch generation check reuses its live family set
for both individual and decomposition identity comparisons. Unrelated malformed metadata
does not veto a family, while malformed metadata on a native member fails the
family closed. Validation waits for every discovered member's original
creation-plus-60-seconds deadline through durable invalidations; linking and
editing do not create a sliding delay.
When GitHub admission defers `--only` before target resolution, the CLI preserves
`Deferred` and the original diagnostic without inventing a target-type failure.
The requested target number must still match; unresolved success remains invalid.
Explicit `--only` startup reuses the complete open-Issue memory cache within its
one-hour TTL. On a miss or expiry, declaration discovery uses the persistent HTTP
cache with its normal freshness/revalidation policy and paginates the Issue list
without per-Issue enrichment or a separate PR listing. Unrelated Issues are not
individually refreshed. Related declarations and the target use strict cache-aware readers
before linking or dispatch; a new body-only declaration omitted from a still-fresh
list becomes discoverable after list expiry. Every subsequent family reconciliation
uses the same lightweight discovery method, including decomposition preflight
and validation; it never calls the enriched repository-wide Issue listing or
fetches unrelated PR connections. Cold caches use only paginated Issue bodies.
An INFO message identifies the family being discovered.
The dashboard records `issue.family-discovery` with the cached discovery source,
live relationship scope, confirmed declared children, and
`authorizes_execution=false`; completion of discovery is not implementation readiness.
The explicit startup pass records `issue.explicit-relationship-discovery` with
`discovery_source=cache-aware-open-issue-list` and `live_scope=target-and-related-family`.
Both discovery stages include `discovery_payload=issue-bodies` and
`relationship_reads=http-cache-freshness`. This describes
the cache policy and lightweight payload, not a claim that every request was a cache hit.
The shared caching-client factory places the diagnostic wire transport inside
Hishel's cache transport (a bare custom transport would disable caching).
Private HTTP responses honor normal expiry and revalidation, with Authorization
and Cookie variants isolated. Wire timeouts and operation identity survive cache
conversion; cache hits require no governor admission. GraphQL operation classification
reads and buffers cache-converted request streams before admission, preserving
the complete outgoing body and the existing read/mutation classification without
logging documents or variables.

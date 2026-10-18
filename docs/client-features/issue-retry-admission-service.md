# Issue retry admission service

Issue-only retry coordination is isolated in `IssueRetryAdmissionService`.
The service consumes durable retry requests from `IssueStageRoutingStore` and
uses the existing `acquire_explicit_retry` ownership boundary; it does not
select or submit providers.

Manual admission accepts authority only for the exact repository, Issue, and
current implementation generation. An earlier unresolved request blocks a
competing request, while replay reconstructs the same request, logical
attempt, predecessor, and ownership reference. The capacity bypass remains
available only to the explicit manual path selected by the existing combined
`--only --force --retry` flags.

Automatic stale-Jules recovery retains its existing durable request while
predecessor implementation activity remains. Once that activity is retired,
the same request may acquire ownership under ordinary capacity rules. The
typed result distinguishes owned, deferred, stale, and failed admission and
always carries the durable authority when one could be established. Provider
acceptance and later publication remain separate downstream transitions.

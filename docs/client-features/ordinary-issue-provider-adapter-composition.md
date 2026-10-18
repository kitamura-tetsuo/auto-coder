# Ordinary Issue provider-adapter composition

Ordinary Issue dispatch keeps candidate ranking in the repository-scoped quota
selector and passes that ordered list to `OrdinaryIssueAdapterComposition`. The
composition resolves configured aliases, constructs the existing local backend
manager or calls the existing remote provider effect owner, and adapts observed
acceptance into the shared `AdapterOutcome` used by `IssueDispatchGuard`.

The boundary receives the Issue snapshot, attempt identity, retry authority,
provider candidates, and narrow effect-owner ports explicitly. It does not own
an `AutomationEngine`, transport client, or state-store replacement. The guard
continues to permit fallback only after `NOT_STARTED`; local or remote
acceptance, local completion, and indeterminate entry stop candidate selection.
Remote acceptance still comes from the provider observer, matching cloud
binding, or matching accepted Codex run rather than action text.

This extraction does not change difficult/high-score routing, synchronous
legacy dispatch, ordinary quota ordering, prompt construction, notifications,
or trace event fields and stages. It is therefore observability-neutral: the
same production selection and local-job events are emitted at the same effect
boundaries, so the dashboard production-to-view contract and its documented
trace scenarios are unchanged.

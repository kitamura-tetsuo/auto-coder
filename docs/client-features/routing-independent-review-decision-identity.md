# Routing-independent review decision identity

Individual Issue specification review and parent/direct-child decomposition
review authorize reuse of a durable `READY`/`BLOCKED` decision using a
semantic policy identity that excludes configured and selected LLM
backend/provider, backend alias, requested or reported model, backend
ordering, fallback membership/order, and quota-based selection. That policy
identity depends only on the versioned review contract
(`VALIDATION_SCHEMA_VERSION` / `DECOMPOSITION_SCHEMA_VERSION`), the exact
review prompt, the allowed finding categories, and the result-schema/
consistency rules. Changing only the configured backend, model, or fallback
route — including a controller restart or configuration reload while a
route change is in effect — never changes an Issue's or parent-family's
validation identity and never causes a compatible durable decision to be
recomputed. `BLOCKED` never silently becomes `READY` from a route change,
and `ERROR` remains non-authorizing and retryable regardless of route.
`AUTO_CODER_SPECIFICATION_VALIDATOR_IDENTITY` (an execution-route override,
not a semantic-policy input) participates the same way: it can change what
routing metadata is recorded, never what a decision durably authorizes.

`AutomationEngine._get_specification_validator`/`_get_decomposition_validator`
cache one lifecycle instance per repository unconditionally for the
process's lifetime; a backend/model/fallback configuration change no longer
rebuilds that cache, and it is not interpreted as a new Review arrival or a
new Implementation generation anywhere family scheduling, startup/recovery,
retained-owner reevaluation, stage routing, or implementation admission
observes these lifecycles.

Execution provenance (the configured route snapshot: alias, provider type,
and model — never credentials) is recorded separately from this policy
identity, only for a decision genuinely produced by a real analyzer call,
captured at the moment that call is dispatched. `ValidationDecision`/
`DecompositionDecision` carry it as `execution_provenance`, alongside
`evaluation_source` (`model`, `local-only`, or `stored-decision-reuse`).
A local-only decision (an Objective-integrity conflict, unavailable
evidence, or a structural-assessment error — no analyzer call happens) never
records provenance and never queries routing configuration to produce one.
Reusing a stored decision always returns its original producing provenance
verbatim; it is never recomputed or relabeled with the currently configured
route.

Because the policy identity's hash previously mixed routing into the same
fields it now excludes, every record persisted before this change is stored
under an old, now-unreachable identity key. Those legacy records are never
deleted, overwritten, or silently promoted: a current-format lookup miss is
followed by a read-only diagnostic scan for on-disk records that share every
non-policy identity field (repository/Issue number/specification digest/
relationship for individual review; repository/parent/children for
decomposition) but carry a different, opaque legacy policy hash. When one or
more such records exist, a distinct `legacy_policy_unproven` diagnostic is
logged and the count is recorded on the fresh decision
(`legacy_candidates_detected`), but review proceeds exactly as an ordinary
fresh review — no legacy record is trusted, and multiple conflicting legacy
`READY`/`BLOCKED` records are never resolved by preferring `READY`. This
migration does not clear Objective anchors, immutable review baselines,
applied-outcome history, repair-round counters, or reissue-required marks,
none of which key on policy identity.

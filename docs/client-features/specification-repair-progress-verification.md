# Specification Repair Progress Verification

Publishing specification or decomposition review findings, withdrawing an
implementation-ready label, and recovering either effect are observation-only
operations. They never reserve an automatic specification-repair round and do
not report that a repair was initiated or completed. A repair round is reserved
only when a caller explicitly supplies an editor to the lifecycle repair hook;
the reservation remains durable when that callback fails or produces no edit.

Consequently, repeated `BLOCKED` publication and recovery remain idempotent and
cannot exhaust the configured repair allowance. The individual standalone,
inherited-child, and decomposition review paths all retain their diagnostic and
readiness effects while leaving the repair count unchanged when no editor is
configured.

An explicit lifecycle repair requires a caller-provided authoritative-state
reader. Before dispatch, the lifecycle durably binds the exact serialized
subject state to the counted generation. After the editor returns or raises,
the lifecycle reads again and durably records `NO_CONTRACT_CHANGE`,
`CONTRACT_CHANGED`, or `UNVERIFIED`, while retaining editor failure separately.
An ended submission or changed subject ownership takes precedence over content
comparison and records `SUPERSEDED`; an invalid current manifest or malformed
read records `UNVERIFIED` rather than apparent contract progress.
Duplicate calls and restart recovery observe the same operation without
dispatching the editor or incrementing the generation again. Neither an editor
return value nor a comment is repair-progress evidence.

This separation is observability-neutral for the dashboard: it removes a false
internal repair authorization that emitted no structured dashboard event and
does not change processing origins, event schemas, or production-to-view
mapping. Existing BLOCKED review-effect traces continue to describe the actual
publication and readiness-withdrawal outcomes.

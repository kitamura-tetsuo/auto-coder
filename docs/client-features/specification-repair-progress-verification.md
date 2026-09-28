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

This separation is observability-neutral for the dashboard: it removes a false
internal repair authorization that emitted no structured dashboard event and
does not change processing origins, event schemas, or production-to-view
mapping. Existing BLOCKED review-effect traces continue to describe the actual
publication and readiness-withdrawal outcomes.

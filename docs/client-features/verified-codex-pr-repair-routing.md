# Verified Codex PR repair routing

Repair effects for a PR associated with accepted Codex Cloud work use the durable,
repository-and-PR-specific publication attribution rather than the source Issue's
current cloud binding, task URLs, cached session fields, or backend preference. The
recorded backend reconstructs the original Codex client while historical Issue
bindings remain unchanged.

Normal CI continuation, unresolved-review repair, adversarial feedback,
merge-conflict follow-up, and upheld-adjudication delivery fail closed when the
attribution is unresolved, unavailable, conflicting, or changes before the final
provider call. Existing head, quota, shutdown, blocker, and repair-budget gates
continue to apply. Recipient-scoped delivery identities keep confirmed and
indeterminate receipts attached to the actual task, independently of PR-body URL
annotation.

The existing repair trace events retain their schemas. They are emitted only after
the verified recipient accepts delivery, so an attribution deferral or successful
body annotation is never presented as provider delivery.

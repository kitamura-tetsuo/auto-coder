# Typed pre-review PR progress policy

Pre-review PR routing is selected by the pure `pr_progress_policy` boundary.
Its input names the repository, PR, immutable head, and either candidate-
collection or in-handler phase. It classifies exact-head CI evidence as passing,
pending, failing, known-empty, unavailable/incomplete, or subject-mismatched and
returns a typed route plus a diagnostic reason.

Candidate collection and direct handler entry intentionally have different
projections. In particular, candidate collection admits known-empty evidence
only for an affirmatively conflicting PR whose remediation is enabled, while
the handler selects the existing manual-dispatch owner for an otherwise
eligible known-empty PR. An enabled, non-exhausted affirmative conflict selects
the existing remediation owner before a CI wait; unknown mergeability never
does so.

The policy neither observes GitHub nor performs effects. Durable CI watches,
deployment approval, dispatch claims, workflow dispatch, conflict repair,
review, final head refresh, and merge remain in their existing owners. A route
only authorizes entry to one of those owners and is never proof that its effect
succeeded. Existing trace stages and payloads are unchanged because the
extraction changes internal route ownership without adding an admission,
outcome, provider, resumption, or structured-event state.

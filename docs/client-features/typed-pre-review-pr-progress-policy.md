# Typed pre-review PR progress policy

Pre-review PR routing is selected by the pure `pr_progress_policy` boundary.
Its input names the repository, PR, immutable head, and either candidate-
collection or in-handler phase. It classifies exact-head CI evidence as passing,
pending, failing, known-empty, unavailable/incomplete, or subject-mismatched and
returns a typed route plus a diagnostic reason.

Candidate collection and direct handler entry intentionally have different
projections. In particular, candidate collection admits known-empty evidence
only for an affirmatively conflicting PR whose remediation is enabled. An
otherwise eligible known-empty PR retains the existing CI wait and durable
recheck at collection; direct handler entry selects the existing durable
dispatch owner so a PR with no checks can make progress. An enabled,
non-exhausted affirmative conflict selects
the existing remediation owner before a CI wait; unknown mergeability never
does so.

The policy neither observes GitHub nor performs effects. Durable CI watches,
deployment approval, dispatch claims, workflow dispatch, conflict repair,
review, final head refresh, and merge remain in their existing owners. A route
only authorizes entry to one of those owners and is never proof that its effect
succeeded. Existing trace stages and payloads are unchanged because the
extraction changes internal route ownership without adding an admission,
outcome, provider, resumption, or structured-event state.

`AutomationEngine` is a collector-phase consumer of this policy. It obtains one
immutable current-head CI observation, passes that same result to the existing
deployment-approval effect owner, and uses the typed collector route—not the
legacy helper Boolean—to decide candidate membership. Author, dependency-bot,
Jules, empty/stale-PR, provider-wait, priority, and Issue-requeue gates remain
independent collector responsibilities. The selected candidate still enters
the handler, where a fresh in-handler policy evaluation protects remediation,
review, repair, and merge effect boundaries.

Diagnostics preserve the evidence distinction as well as the route. A
`KNOWN_EMPTY` snapshot is reported as having no current CI observations, never
as an executing workflow. The collector then separately reports whether its
typed route excluded or retained the candidate; genuine pending evidence keeps
the existing in-progress wording. Direct handler entry describes the absent
observation accurately and enters the existing claim-protected workflow
dispatch effect.

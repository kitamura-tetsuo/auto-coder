# Evidence-bound individual Issue rereview

Individual specification validation retains a versioned evidence envelope for
every accepted decision. The envelope binds the exact repository and Issue,
ordered Requirement manifest and validity, title/body, reconciled role and
parent, related contracts, semantic policy and rerun authority, fixed Objective
and first baseline, findings, dispositions, coverage, and original execution
provenance. Findings receive controller-owned identities independent of their
wording and Requirement references.

An exact current identity reuses its durable decision without a semantic model
call. A changed identity uses the latest durably accepted compatible predecessor
in acceptance order and sends its exact evidence plus a lossless field-level
before/after delta to the production analyzer. Changed role or parent, semantic
policy, rerun authority, or missing/corrupt legacy evidence selects a full
review instead. Provider/model routing is provenance and does not invalidate
compatible semantic evidence.

Incremental output must adjudicate every predecessor finding as `RESOLVED`,
`STILL_VALID`, `REGRESSED`, or `UNVERIFIED`, and account for every current
Requirement plus the contract-wide boundary as freshly reviewed, carried with a
specific no-impact basis, or unresolved. Unknown/duplicate references,
unsupported carry-forward, incomplete coverage, contradictory dispositions,
and unverified evidence fail closed as `ERROR`. A current `READY` decision is
accepted only with complete coverage and no remaining finding. The analyzer may
broaden uncertain impact to fresh or full review and may still report a newly
demonstrated material defect, but does not restart unrelated searches after the
affected boundaries are checked.

Standalone and reconciled-child Review-lane calls use this same lifecycle and
store. The durable audit report exposes the review mode, predecessor key,
assessed delta, stable finding identities and dispositions, and coverage/no-impact
reasons; these fields are evidence, not an alternate readiness authority.

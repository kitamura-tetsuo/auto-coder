# ChatGPT review-adjudication authoring

The repository provides a reusable ChatGPT authoring prompt and an operator
guide for analyzing and explicitly delegating publication of a complete review
root through GitHub's read and top-level review-comment reply operations. The
workflow has no CLI or local-service dependency, uses the production reader's
copy-ready projection and shared decision renderer, and treats publication as
awaiting Auto-Coder rather than as a completed repair.

The projection exposes the bound target and root revision, context identity,
head/base revisions, contract and Objective-scope identities, current graph
tips, permitted numeric adjudicator IDs, reader lifecycle result, and observation
revision. The prompt requires a fresh preflight and fails closed on incomplete,
unavailable, retired, changed, or unauthorized evidence. Decisions remain
append-only, and ambiguous publication is recovered only by locating the exact
attempted payload in the exact thread.

Reader unavailability and permanent retirement are appended to the bound root
thread as non-authorizing lifecycle projections. They remain GitHub-readable
when later contract reads fail, and a restoration of matching values cannot
turn a retired context back into authority.

Semantic authoring evaluations are advisory and separate from deterministic
transport and production-reader conformance tests. This authoring layer emits no
new processing-trace schema or dashboard state; admitted decisions continue
through the existing review-adjudication effect stages, so the documented
dashboard observability contract is unchanged.

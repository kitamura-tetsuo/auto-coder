# Causally scoped CI observation snapshots

Transport-independent CI reads produce immutable snapshots scoped to the API
origin, repository, pull request, exact head SHA, source representation, a fresh
opaque read-cycle identity, and the captured relevant-invalidation epoch.
Workflow facts preserve workflow, run, and explicit attempt identities; check
facts preserve App and check identities plus only explicitly available workflow
associations. Human-readable names and timestamps are diagnostic metadata and
never execution identity.

Availability distinguishes complete known facts, complete known-empty facts,
partial evidence, unavailable reads, throttled reads, and superseded reads.
Unavailable current reads may retain previous facts only as non-authoritative
diagnostics and never turn missing evidence into an empty or passing result.
Unknown attempts and associations remain unresolved, while exact higher attempts
at the same head are distinct activity and fence late older completions.

One bounded in-memory slot authorizes reuse only inside the same active read-only
phase and exact scope, request representation, and epoch. Every phase and read
uses a new opaque identity independent of durable queue generations. A newer
read, relevant invalidation, head or phase change, mutation boundary, governor
wait, or external/LLM work fences earlier reads. Late completions can occupy only
a bounded diagnostic slot and cannot overwrite current facts. Persisted
diagnostics after restart require a fresh authoritative cycle before reuse.
Constructing, publishing, or reusing these observations has no GitHub or provider
side effects and does not itself compute the required-check verdict.

Production GitHub CI integration targets check runs and workflow runs by exact
head and retrieves all pages as one complete observation. Identical authenticated
representations coalesce inside candidate-selection phases; phases do not survive
mutation or external-work boundaries. Deployment approval is a separate serialized
policy consumer which freshly revalidates the run attempt and pending environment
set before submitting an approval, and remembers confirmed or indeterminate delivery.

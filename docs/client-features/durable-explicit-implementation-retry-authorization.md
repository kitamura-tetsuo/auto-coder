# Durable explicit implementation retry authorization

An operator retry can be accepted as a durable request independently of an
Issue's semantic Implementation generation. Each non-empty request identity is
atomically bound to its repository, positive Issue number, exact generation,
and a newly allocated attempt identity. Replaying the same binding returns the
same record; reusing the request identity for different inputs fails without
changing it. Acceptance remains `pending` and neither reserves capacity nor
creates a local execution, provider task, pull request, or owned-start fact.

The production adapter consumes pending authority at the existing durable
local-execution boundary. The slot record captures request, attempt, and
generation identities with the execution, allowing reconstruction to finish a
crash-interrupted projection without creating another attempt. Per-owner
cross-process serialization orders acquisition, contention, and authoritative
generation invalidation. Capacity or a live local execution returns the request
to `pending`; a later call in the same process may retry it. An acquired or
invalidated request never becomes pending again. A retry is a request-scoped
exception to an existing generation tombstone: it neither deletes that history
nor changes semantic-generation equality.

Durable query and enumeration expose the binding, its
`pending`/`owned`/`invalidated` state, the acquired execution
reference when one exists, and refusal or invalidation detail. Unknown,
malformed, contradictory, unreadable, or unwritable authority fails closed.
The state contains no provider credential and distinguishes ownership from any
later provider invocation or completion.

This layer intentionally does not activate a CLI/controller route, select or
invoke a provider, change capacity or hierarchy policy, or create retry
requests from ordinary wakes, review reruns, failures, retirement, or elapsed
time. It adds no production trace or dashboard event: acceptance and ownership
are durable authority operations only, so the existing production-to-dashboard
event schema and `docs/dashboard-observability.md` scenarios are unchanged.

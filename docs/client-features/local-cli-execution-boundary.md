# Local CLI execution boundary

Each Git-backed local backend call has controller-owned execution state bound to
the private repository invocation. The state records the actual backend type,
effective edit mode, private workspace identity, policy-violation status, and
whether the complete writer lifetime has settled. It is held outside model output
and cannot be replaced by completion text or an agent-provided flag.

Boundary state is scoped by context to one invocation, so concurrent invocations
cannot settle or invalidate one another. This state abstraction does not itself
confine filesystem or network access and does not prove that a provider's complete
descendant tree stopped; those facts require an enforcing launcher before this
evidence can authorize promotion. Positive violations, once reported by that
launcher, remain terminal even if the provider later exits successfully.

Explicit continuation failures are not redirected into fresh invocations. Effective
mode is resolved before boundary construction, including clients constructed for
no-edit operation, so controller evidence cannot advertise editable authority for a
read-only invocation.

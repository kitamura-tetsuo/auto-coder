# Local CLI execution boundary

Each Git-backed local backend call has controller-owned execution state bound to
the private repository invocation. The state records the actual backend type,
effective edit mode, private workspace identity, policy-violation status, and
whether the complete writer lifetime has settled. It is held outside model output
and cannot be replaced by completion text or an agent-provided flag.

A local result is eligible for handoff only after its writer tree has terminated
and no policy violation or boundary failure was recorded. Positive violations stay
terminal even if the provider later exits successfully. Boundary state is scoped by
context to one invocation, so concurrent invocations cannot settle or invalidate
one another.

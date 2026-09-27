# Local CLI execution boundary

Each Git-backed local backend call has controller-owned execution state bound to
the private repository invocation. The state records the actual backend type,
effective edit mode, private workspace identity, backend outcome, filesystem and
publication enforcement, writer completion, and violation-observation status. Each
runtime fact starts unknown and must be positively established by an authoritative
producer for the same invocation. It is held outside model output and cannot be
replaced by completion text or an agent-provided flag.

Boundary state is scoped by context to one invocation, so concurrent invocations
cannot settle or invalidate one another. This state abstraction does not itself
confine filesystem or network access and does not prove that a provider's complete
descendant tree stopped; those facts require an enforcing launcher before this
evidence can authorize promotion. Positive violations, once reported by that
launcher, remain terminal even if the provider later exits successfully.

An ordinary successful provider return records only successful backend completion.
It remains a usable legacy result, but its enforcement and writer facts stay unknown
and it cannot authorize a certified confined result. The aggregate authorization
query succeeds only with matching positive evidence for every required fact; no-edit
evidence can never authorize edit promotion.

Explicit continuation compatibility failures are not redirected into fresh
invocations. Effective mode is resolved before boundary construction, including
clients constructed for no-edit operation, so controller evidence cannot advertise
editable authority for a read-only invocation.

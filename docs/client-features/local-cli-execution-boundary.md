# Local CLI execution boundary

Each Git-backed local backend call has controller-owned execution state bound to
the private repository invocation. The state records the actual backend type,
effective edit mode, private workspace identity, unique turn identity, exact
provider-session association, backend outcome, filesystem and publication
enforcement, writer completion, and violation-observation status. Each
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
and it cannot authorize a certified confined result. The aggregate operational
success query requires positive backend completion, filesystem enforcement, writer
settlement, and violation observation. Publication enforcement remains descriptive
legacy evidence but is not required for operational success. No-edit evidence can
never authorize edit promotion, and a provider session identifier is metadata—not
continuation admission or permission to reuse a result root.

Codex turns use this boundary at the real command launch. The controller strips
ambient Git target overrides, requires the command cwd to equal the immutable
private result root, installs the Linux filesystem policy, and launches the CLI in
its invocation-owned cgroup. A provider return is successful only after the cgroup
is positively empty and the direct child is reaped. Production deployments provide
the non-root child credentials with `AUTO_CODER_LOCAL_WORKER_UID` and
`AUTO_CODER_LOCAL_WORKER_GID`; missing or unusable cgroup/Landlock prerequisites
cause refusal before the CLI starts rather than an editable fallback.

Explicit continuation compatibility failures are not redirected into fresh
invocations. Effective mode is resolved before boundary construction, including
clients constructed for no-edit operation, so controller evidence cannot advertise
editable authority for a read-only invocation.

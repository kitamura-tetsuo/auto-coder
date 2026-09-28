# Invocation-owned local process supervision

Finite local CLI calls can be launched through `InvocationProcessSupervisor`. The
launcher creates a unique cgroup-v2 owner before executing provider code, installs
controller-supplied policies against that same owner, and places the child in the
cgroup between `fork` and `exec`. Cgroup membership follows forks, double-forks,
session changes, and reparenting, so process IDs, a working directory, and a
one-time process snapshot are not treated as ownership evidence.

The production profile is Linux with Landlock ABI 3 or newer and a unified cgroup-v2 hierarchy
and a root-owned `/sys/fs/cgroup/auto-coder` subtree. A privileged controller must
be able to create child cgroups and must configure an explicit, non-root worker UID
and GID. The launcher joins the child to its invocation cgroup and then permanently
drops its supplementary groups, GID, and UID before `exec`. The subtree must not be
writable by those worker credentials, preventing a provider from creating or
joining a sibling cgroup. `cgroup.kill` is mandatory; kernels lacking it are
rejected rather than using reusable numeric PIDs as termination identities. These
capabilities are checked before provider submission. A missing controller,
unsafe delegation or credentials, unsafe/reused invocation identity, failed policy
installation, or failed owned launch returns `pre-start-unavailable` without an
unowned fallback.

Writer state is controller-owned and progresses from `not-started` through
`active` and `stopping` to either `positively-stopped` or `termination-unknown`.
Positive completion is emitted only after the kernel's `cgroup.events` reports
`populated 0` and the direct child is reaped. Prompt delivery and stdout/stderr
draining run concurrently with lifecycle observation, so pipe backpressure cannot
block timeout or cancellation. Timeout, cancellation, provider
failure, and writer settlement remain independent result facts. Confirmation
failure retains the invocation's original outcome, cgroup and result paths, and
cannot authorize replacement. Positive settlement likewise does not dispose of a
result directory or authorize a retry: the controller must make a separate
replacement decision.

The deterministic conformance coverage is in
`tests/test_invocation_process_supervisor.py`; run it with
`bash scripts/test.sh tests/test_invocation_process_supervisor.py`. Production
startup compatibility can be checked by ensuring `/sys/fs/cgroup/cgroup.controllers`
and `cgroup.kill` exist, the controller runs as root, a non-root worker identity is
configured, and `/sys/fs/cgroup/auto-coder` is root-owned and not worker-writable.
The tested profile is CPython 3.12 on Linux cgroup v2 with Landlock ABI 3 or newer.
The documented channel Compose profile supplies this profile using `cgroup: host`,
a read-write `/sys/fs/cgroup` mount, privileged controller execution, and the
image's fixed non-root worker UID/GID 65532. These privileges are required for the
controller to create root-owned invocation cgroups; they are not optional provider
permissions.

# Invocation-owned local process supervision

Finite local CLI calls can be launched through `InvocationProcessSupervisor`. The
launcher creates a unique cgroup-v2 owner before executing provider code, installs
controller-supplied policies against that same owner, and places the child in the
cgroup between `fork` and `exec`. Cgroup membership follows forks, double-forks,
session changes, and reparenting, so process IDs, a working directory, and a
one-time process snapshot are not treated as ownership evidence.

The production profile is Linux with a unified cgroup-v2 hierarchy and a delegated,
writable `/sys/fs/cgroup/auto-coder` subtree. The deployment must permit creating
and removing child cgroups and writing `cgroup.procs`; Linux 5.14 or newer provides
`cgroup.kill`, while older cgroup-v2 kernels use repeated member termination. These
capabilities are checked before provider submission. A missing controller,
read-only/non-delegated hierarchy, unsafe/reused invocation identity, failed policy
installation, or failed owned launch returns `pre-start-unavailable` without an
unowned fallback.

Writer state is controller-owned and progresses from `not-started` through
`active` and `stopping` to either `positively-stopped` or `termination-unknown`.
Positive completion is emitted only after the kernel's `cgroup.events` reports
`populated 0` and the direct child is reaped. Timeout, cancellation, provider
failure, and writer settlement remain independent result facts. Confirmation
failure retains the invocation's original outcome, cgroup and result paths, and
cannot authorize replacement. Positive settlement likewise does not dispose of a
result directory or authorize a retry: the controller must make a separate
replacement decision.

The deterministic conformance coverage is in
`tests/test_invocation_process_supervisor.py`; run it with
`bash scripts/test.sh tests/test_invocation_process_supervisor.py`. Production
startup compatibility can be checked by ensuring `/sys/fs/cgroup/cgroup.controllers`
exists and `/sys/fs/cgroup/auto-coder` is a writable delegated subtree. The tested
profile is CPython 3.12 on Linux cgroup v2 (kernel 5.14 or newer preferred).

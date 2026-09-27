# Invocation-local filesystem confinement

The invocation process supervisor can install `LandlockFilesystemPolicy` before it
releases a provider executable. The controller supplies an immutable invocation
identity, effective mode, private result repository, owned runtime directories,
protected caller or peer paths, and optional read-only runtime inputs. All paths
are required to exist and be absolute. The installer resolves them, rejects
protected/read-only aliases, and pins approved roots with open descriptors before
the child exists, so replacing a checked symlink cannot retarget a rule.

On Linux, the child joins its invocation-owned cgroup and then installs a Landlock
ruleset with `no_new_privs` before `exec`. Editable calls may mutate only the private
result and owned runtime roots. No-edit calls can inspect the repository but cannot
mutate repository, Git, scratch, or runtime paths; trusted bookkeeping remains in
the controller rather than granting the provider a writable root. The kernel
applies the policy to shells, interpreters, absolute
executables, Git alternate-directory options, hooks, and every descendant. Writes
through path traversal, symlinks, hard-link creation, rename/refer operations, and
newly opened descriptors are checked at the actual filesystem operation.

Landlock support and every policy prerequisite are checked before provider code is
submitted. Unsupported kernels, missing roots, unsafe modes, or aliasing return a
`pre-start-unavailable` result rather than launching without confinement. The
cgroup owner retains the policy for detached descendants until definitive writer
shutdown; termination uncertainty therefore retains both process and filesystem
ownership. Read visibility can be narrowed with controller-selected roots for a
later credential/publication stage and cannot be widened by provider environment
variables or output.

The deterministic Linux regression suite is
`tests/test_filesystem_confinement.py`; run it with
`bash scripts/test.sh tests/test_filesystem_confinement.py`. This producer changes
no dashboard event schema or processing outcome taxonomy: it supplies the existing
local execution boundary's filesystem-enforcement fact and the supervisor's
existing `pre-start-unavailable` outcome.

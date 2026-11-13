# Local Codex direct-process completion

Local Codex auditing, implementation, and retained-session continuation use direct
CLI process completion instead of mandatory all-descendant termination proof.
This is a permanent Codex-only policy, not a PR-specific exception or a fallback
selected after confinement fails. OpenCode and other backend completion criteria
are unchanged. Cloud Codex is not this local adapter.

The launcher requires a non-root controller, the invocation's private repository
and private HOME/runtime paths, and successful Landlock filesystem-policy
installation. It does not require a privileged controller, worker identity
switch, writable host cgroup, or cgroup.kill for Codex. No-edit remains enforced
by the mandatory filesystem policy: repository writes are denied while private
runtime writes are allowed. The effective CLI sandbox argument is selected by
the existing supervised launch path; it is not independent proof of read-only
behavior. Failed policy installation must not fall back to an unconfined command.

The boundary records direct_process_completion separately from writer_completion.
Only the observed direct-process exit establishes the former; it does not
establish the latter. Descendant termination remains explicitly unproven.
Timeout, cancellation, unsuccessful provider exit, identity mismatch, and policy
monitoring failures remain failures. Termination attempts are best effort, not
an authoritative proof that every detached descendant stopped.

For Codex, result handoff, retained-root reuse, and lease release use the direct
completion fact. They still require their other existing conditions, including
backend success for successful results, matching invocation/turn/workspace,
completed observation, and a one-use lifecycle decision for continuation.
No-edit evidence never authorizes editable publication. OpenCode still requires
positive writer settlement. Strong audit still runs and must actually produce
valid current-head/contract evidence; ordinary validation PASS is not a substitute.
CI and the semantic review requirements are unchanged.

This policy accepts a residual race: an unproven descendant may remain after the
CLI exits and overlap result handoff, cleanup, or later root reuse. Landlock's
inherited mutation restrictions are retained, but these restrictions are not a
complete descendant-liveness or read-visibility guarantee. PID snapshots,
unchanged Git state, and green tests do not establish all-descendant termination.

Focused boundary/continuation coverage is in
`tests/test_codex_direct_completion_boundary.py`,
`tests/test_local_execution_boundary.py`, and
`tests/test_local_session_continuation.py`. Launcher and filesystem regressions
must separately validate the actual non-root launch and no-edit denial path.

`tests/test_codex_noedit_runtime.py` drives the pinned real Codex executable,
production Strong Audit path, and a local controlled inference endpoint through
PASS, FINDINGS, INCONCLUSIVE, malformed output and source/Git/caller/peer write
attempts. Its Docker profile is non-root with all capabilities dropped and
no-new-privileges, without a writable cgroup mount. In an already isolated
non-root environment with the pinned executable, run
`AUTO_CODER_CODEX_HOST_RUNTIME=1 bash scripts/test.sh tests/test_codex_noedit_runtime.py`.
This tests the audit boundary without using production inference credentials or
changing production PR state. It does not replace live deployment verification.

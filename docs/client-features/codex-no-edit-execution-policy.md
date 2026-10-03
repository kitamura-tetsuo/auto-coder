# Codex No-Edit Execution Policy

No-edit Codex backends do not require
`--dangerously-bypass-approvals-and-sandbox`. Editable Codex backends must use
an unattended policy: that bypass flag, `--full-auto`, or explicit
`--sandbox workspace-write` with `--ask-for-approval never`. For every no-edit
invocation, including custom `backend_type = "codex"` aliases, auto-coder
removes dangerous/YOLO flags and initially selects `--sandbox read-only`,
`--ask-for-approval never`, and the configured safe approval reviewer override.

For a current controller-created private workspace and matching no-edit execution
boundary, the supervised command launcher replaces that selection with
`--sandbox danger-full-access`. Auto-Coder's mandatory Landlock/ptrace policy
continues to enforce no-edit authority before provider code executes. This avoids
Codex 0.159.2's nested Linux sandbox and its shared daemon directory without granting
writes to shared `/tmp`. Repository, Git metadata, caller state, and peer workspaces
remain protected; only invocation-owned runtime paths are writable.

There is no sandbox capability probe, cached fallback decision, client opt-in, or
`.git` path heuristic. Unsupervised calls retain Codex read-only mode. Missing
invocation bindings, mismatched modes, failed policy/owner setup,
and uncertain writer settlement refuse execution or fail the result. Denied write
attempts remain blocked and logged but do not fail an otherwise successful turn. Final messages
are transferred by the controller only after positive writer settlement; CLI exit 0
alone does not establish review acceptance.

Observability is neutral: this adapter changes the inner Codex sandbox argument
and disables optional Git index refreshes in that same no-edit launch. The
existing supervisor supplies filesystem and writer evidence and
existing failures. Denied operations alone now preserve the successful backend
outcome, while provider selection and dashboard trace schema remain unchanged.

Codex no-edit policy also pins the kernel `/dev/null` character device for sink
I/O, which Git requires for `git rev-parse HEAD` under Codex's shell runtime.
This grants no writable `/dev` directory or persistent file-state allowance.
The private `CODEX_HOME` directory is created even with API-key authentication.
Supervised no-edit Codex also receives `GIT_OPTIONAL_LOCKS=0`, so repository
inspection with `git status` does not attempt an optional `.git/index.lock`
write. Required Git mutations remain denied by the same filesystem policy.
The production image pins Codex 0.159.2. Real-CLI conformance runs with the
existing `opencode_live` suite in its privileged controller/non-root worker
container profile (`tests/test_codex_noedit_runtime.py`); it drives Strong Audit
through the production factory, executor, client, supervisor, and durable state,
including denied writes followed by accepted exit-0 payloads and malformed
non-authoritative payloads.

A caught write denial is advisory even if Codex later emits PASS and exits 0.
The supervisor preserves successful completion and the boundary retains the
policy-violation flag without invalidating result authorization. Denial diagnostics
remain in the executor result and interaction log and are emitted as warnings.
The provider payload still passes the normal review identity and verdict checks;
malformed results, actual CLI failures and failed enforcement remain failures.
Valid FINDINGS continue through the normal accepted-finding transition, including
its existing requirement for a fresh ordinary review after repair.

Codex's runtime PATH aliases may point to its protected executable. The syscall
guard checks the directory entry for `unlink`/`unlinkat`, so removing an owned
runtime alias is permitted without granting mutation rights to its referent.
Removing a caller-owned alias pointing into the runtime is still denied.

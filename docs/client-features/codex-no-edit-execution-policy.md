# Codex No-Edit Execution Policy

No-edit Codex backends do not require
`--dangerously-bypass-approvals-and-sandbox`. Editable Codex backends must use
an unattended policy: that bypass flag, `--full-auto`, or explicit
`--sandbox workspace-write` with `--ask-for-approval never`. For every no-edit
invocation, including custom `backend_type = "codex"` aliases, auto-coder
removes dangerous/YOLO flags and enforces `--sandbox read-only`,
`--ask-for-approval never`, and the configured safe approval reviewer override.

## Supervised no-edit sandbox delegation

A no-edit Codex call selects `--sandbox danger-full-access` only when it runs
inside the current invocation's supervised launch: the call is routed through
`InvocationProcessSupervisor`, its no-edit boundary resolves to backend type
`codex`, and the boundary binds the same invocation and private workspace as the
current local workspace (`utils.is_qualified_supervised_noedit_codex`). The
supervisor refuses to start the provider unless the outer Landlock/ptrace policy
is installed on the child, so Codex's nested bubblewrap sandbox (which cannot
start under the unprivileged worker and fixed `/tmp/codex-daemon-<UID>`) is
redundant. Any other no-edit call keeps `--sandbox read-only`. No controller-side
`codex sandbox` probe or client opt-in Boolean is used; the shared `/tmp` is never
made writable. Editable Codex mode and other backend types are unchanged.

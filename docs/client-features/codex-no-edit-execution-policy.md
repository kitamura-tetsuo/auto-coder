# Codex No-Edit Execution Policy

No-edit Codex backends do not require
`--dangerously-bypass-approvals-and-sandbox`. Editable Codex backends must use
an unattended policy: that bypass flag, `--full-auto`, or explicit
`--sandbox workspace-write` with `--ask-for-approval never`. For every no-edit
invocation, including custom `backend_type = "codex"` aliases, auto-coder
removes dangerous/YOLO flags and enforces `--sandbox read-only`,
`--ask-for-approval never`, and the configured safe approval reviewer override.

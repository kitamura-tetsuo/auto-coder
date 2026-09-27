# Muse MSP session continuation

The Muse backend uses a separately owned, non-interactive `muse serve` MSP host
for every invocation. A fresh call starts one root session in the operation's
resolved execution worktree, sends exactly one rendered user turn, and exposes
the host-issued session ID. Explicit continuation starts a replacement host,
resumes exactly the requested durable session, verifies its session identity,
workspace, model, and pending-request state, and then sends one new turn.

Auto-Coder accepts only the final assistant message belonging to a successfully
completed new turn. Protocol failures, identity or workspace mismatches,
pending interactive requests, host death, and timeouts fail continuation; the
backend manager may subsequently perform its existing fresh fallback, but does
not report that fallback as resumed continuity.

No-edit is applied independently to every host by disabling write, shell, and
approval capabilities before session start or resume. Existing repository and
Git-state snapshots remain authoritative, and each owned host is closed or
terminated on every handled outcome. CLI options with no exact MSP equivalent
are rejected before session or turn submission rather than silently ignored.

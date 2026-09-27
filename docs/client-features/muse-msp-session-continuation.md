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

No-edit is applied independently to every host by passing only
`--disable-write` and `--disable-shell` to `muse serve`, then establishing
`denyUnmatched` approval over MSP before the turn. Existing repository and
Git-state snapshots remain authoritative, and each owned host is closed or
terminated on every handled outcome. CLI options with no exact MSP equivalent
are rejected before session or turn submission rather than silently ignored.

The adapter accepts Muse host version `1.3.0` with MSP schema version `1` and
the pinned schema fingerprint. Session and turn commands carry UUIDv7 command
identities; their returned state and acknowledgements are checked before work
continues. Model selection belongs to fresh-session setup, reasoning effort
belongs to turn submission, and editable sessions omit an approval-mode default.
Workspace-trust requests are rejected because PR review has no independent
authorization that can grant that trust.

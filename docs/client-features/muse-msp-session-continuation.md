# Muse MSP session continuation

The Muse backend uses a separately owned, non-interactive `muse serve` MSP host
for every invocation. A fresh call starts one root session in the operation's
resolved execution worktree, sends exactly one rendered user turn, and exposes
the host-issued session ID. Explicit continuation starts a replacement host,
resumes exactly the requested durable session, verifies its session identity,
workspace, model, and pending-request state, and then sends one new turn.

Auto-Coder accepts only the final assistant message belonging to a successfully
completed new turn. Protocol failures, identity or workspace mismatches,
pending interactive requests, rejected or unverifiable approval changes, host
death, timeouts, and quota exhaustion fail continuation. The backend manager
propagates all Muse protocol, session-state, and execution continuation errors
without rotating to a fallback backend or starting a fresh session, because a
fresh call cannot preserve the explicitly requested conversation.

Editable Muse turns run in the shared controller-owned private repository. Local Git
operations—including staging, commits, branches, stashes, rebases, and private
worktrees—are valid coding state and are not audited, reset, unstaged, or rejected
merely because private Git state changed. The accepted generation is delivered by
the shared local-result handoff rather than by selecting files from the final private
HEAD.

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
authorization that can grant that trust. Every successful turn also requires the
owned Muse host to exit successfully; a completed-looking protocol exchange followed
by a nonzero process status is a failed invocation.

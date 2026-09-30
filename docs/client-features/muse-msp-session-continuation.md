# Muse MSP session continuation

The Muse backend uses a separately owned, non-interactive `muse serve` MSP host
for every invocation. A fresh call starts one root session in the operation's
resolved execution worktree, sends exactly one rendered user turn, and exposes
the host-issued session ID. Explicit continuation starts a replacement host,
resumes exactly the requested durable session, verifies its session identity,
workspace, model, and pending-request state, and then sends one new turn.
Every host handshake identifies Auto-Coder as `auto_coder`, a protocol machine
identifier containing only lowercase ASCII letters and an underscore. An
initialization refusal is surfaced with the host's error details and prevents
all session and turn messages; it also clears any prior last-session result.

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
HEAD. An editable client call without the matching controller-owned local execution
boundary is refused before the MSP host starts, so callers cannot execute directly in
their checkout.

No-edit is applied independently to every host by passing only
`--disable-write` and `--disable-shell` to `muse serve`, then establishing
`denyUnmatched` approval over MSP before the turn. Existing repository and
Git-state snapshots remain authoritative, and each owned host is closed or
terminated on every handled outcome. CLI options with no exact MSP equivalent
are rejected before session or turn submission rather than silently ignored.

Compatibility is determined by the explicit supported `(schema version, schema
fingerprint)` pairs, currently `(1,
sha256:b1e6676d624e116e2c1b150fec3192200d2cbca8ed79898e44f8921759c7872f)`
and `(1,
sha256:e0e163db6ccf00dbe68402ce55d6319b3edc33c421f31e9583b587b2de8a118f)`.
The latter was captured from the official Muse Code 1.4.1 build
`1.4.1-R4503.1` (`muse-build` commit
`35815c253477406321e3f5595195becc25e3907a`), in its `initialize.schema`
response, corroborated by the same binary's stable `muse schema
generate-json-schema` manifest. The observed Linux x86-64 binary has SHA-256
`c6db294799a190ca380da274beb3b9c0e160e0da9681a3d364ce8b0e5fa3a4bc`.
Host version strings are retained for diagnostics but are not
compatibility authority. Session and turn commands carry UUIDv7 command
identities; their returned state and acknowledgements are checked before work
continues. Model selection belongs to fresh-session setup, reasoning effort
belongs to turn submission, and editable sessions omit an approval-mode default.
The 1.4.1 host may record the selected `muse-spark-1.3` model as its effective
`muse-spark-1.3-contributor` model after the first turn. Resume accepts that
specific observed alias resolution for the 1.4.1 schema; other model changes
remain incompatible.
Completed assistant output uses the admitted schema's `agentMessage` item shape;
the legacy supported profile's `message`/`assistant` shape remains accepted.
Workspace-trust requests are rejected because PR review has no independent
authorization that can grant that trust. Every successful turn also requires the
owned Muse host to exit successfully and every process in its owned process group to
be positively stopped; a completed-looking protocol exchange followed by a nonzero
process status or unsettled descendant writer is a failed invocation. A terminal
notification for a different turn is rejected immediately as a protocol error rather
than being allowed to age into a timeout.

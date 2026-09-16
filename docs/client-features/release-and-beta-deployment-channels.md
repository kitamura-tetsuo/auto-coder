# Release and beta deployment channels

Successful `main` builds publish one immutable GHCR image tagged by commit and
advance `beta`; failed builds cannot move that tag. The operator-only promotion
workflow derives and verifies the current tested `beta`, records its manifest
under the immutable `release-<SHA>` history tag, and advances `release` without
rebuilding. A separate rollback workflow accepts exactly one explicit target:
a validated dated GitHub Release tag, or a full SHA from pre-catalog immutable
history. It pins the selected digest and moves only `release` without rebuilding.
Registry reads validate concrete SHA-256 digests and fail closed on transport,
authentication, malformed-output, and ambiguous errors. Promotion creates an
immutable history tag only when the registry explicitly confirms it is absent;
creation uses an atomic conditional registry write, so an existing or
concurrently created history tag is validated and is never rewritten.
`compose.channels.yml` pins both
instances by digest and gives release and beta separate durable state, workspace,
cache, and log roots. Each externally deployed process records its channel and
artifact identity, disables internal package updates, and fails closed unless a
shared ownership registry assigns its repository to that channel. Repository
assignment is atomic and cannot change channels while the old channel's durable
implementation-slot state is active or unreadable.

This file documents the client-facing features and tools provided by auto-coder.

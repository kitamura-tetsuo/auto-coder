# Generation-safe local session continuation

Local continuation is an explicit operation over a controller-retained private
workspace. OpenCode is the first concrete adapter admitted to this path: its
provider preflight proves that the requested session belongs to the exact command
directory. Adapters without that capability fail before provider submission, and
an explicit continuation is never replaced by a fresh task or another backend.

The manager retains the provider session, invocation identity, private root,
caller identity, and preceding turn evidence together. Before each later turn it
requires positive writer settlement and consumes an exact generation-specific
live-root reuse decision. Missing, denied, stale, mismatched, released, or
concurrently active state fails closed. Path equality and a session string are not
substitutes for the retained binding.

Every admitted continuation creates a new local execution boundary and resolves
its effective edit mode again. Its own provider completion, session identity, and
writer evidence replace the predecessor evidence used by the next reuse decision;
earlier evidence cannot make the current turn successful. Releasing a retained
session drops only that session's lease, so delayed lifecycle work cannot dispose
an actively retained generation.

A manager keeps unreserved continuation context for its latest fresh local task. Before a
new fresh local invocation it releases earlier unreserved sessions, including their
snapshot and repository, rather than accumulating one unused clone per task.
A session with an explicit positive pending reuse decision survives unrelated
fresh tasks. After that one-turn decision is consumed, a later fresh task retires
the root unless the controller authorizes another reuse or explicitly releases
it sooner. A fresh provider session cannot overwrite the identity of a still
reserved session. An active
turn or uncertain writer settlement refuses replacement before another clone or
provider call. Explicit continuation reuses the latest root and does not trigger
this retirement. `close()` releases settled idle sessions; garbage collection of
an abandoned manager also drops their leases and closes retained directory file
descriptors. These releases remain subject to execution/handoff ownership, so an
active or unsettled writer root cannot be removed. Hard process termination cannot
run these cleanup hooks; workspace capacity preflight prevents repeated copies
from consuming the remaining reserve when orphaned roots remain.

Each admitted retained turn resets its execution lease. Failure replaces the
predecessor with that turn's evidence, so an earlier settled turn cannot be used
to delete a root whose current writers are unconfirmed. Recovery retention after
failed handoff must explicitly hold a session lease; the finished/abandoned
handoff lease itself does not retain a workspace indefinitely.

After handoff, checkpoint advancement compares the shared source scope rather than
new ignored build output. Preserved baseline paths and privately tracked files
remain validated, including paths now covered by an ignore rule. Ignored caller
context still participates in the staleness guard. See
[private local Git workspaces](private-local-git-workspaces.md) for the scope rules.

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

# Shared Runtime Lock Directories

Implementation-slot store and owner locks use persistent repository- and
store-specific paths below the configured runtime root. When the protected
state directory supplies a shared group, newly created namespace directories
are group-owned and mode `02770`; lock files remain mode `0660`.

Concurrent authorized identities may observe a new namespace level before its
creator has applied the shared group and final mode. Consumers wait for that
bounded publication window before creating the next level. A namespace that
does not become writable still fails with its affected path, and existing
directory metadata is never changed. Establishment does not change the process
umask or fall back to a private namespace.

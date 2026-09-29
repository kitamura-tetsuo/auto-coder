# Repository-owned pending-work execution

A serving pending-work scheduler is explicitly bound to the `owner/repository`
target passed to `AutomationEngine.start_automation`. Repository names are compared
after surrounding ASCII whitespace is removed and ASCII letters are lowercased;
malformed names are rejected. The retained identity text is never rewritten.

The bound store view limits due work, interrupted recovery, next-deadline selection,
status snapshots, manual retries, and identity mutations to that repository. The
scheduler also checks ownership immediately before claiming an obligation, while all
six production handlers check it again before repository reads or stage effects.
Foreign records remain untouched in the shared legacy SQLite database and remain
available to a controller bound to their repository.

Repository-specific storage is resolved beneath
`~/.auto-coder/repositories/<sha256-owner-key>/github_pending_work.db`. A durable
owner record prevents a path collision from relabeling a database. A fresh
repository is initialized atomically only after an authoritative legacy read finds
no owned rows; otherwise readiness reports `MIGRATION_REQUIRED`.

Operators cut over retained work with
`auto-coder pending-work migrate --repository OWNER/REPO --offline`. The flag is an
acknowledgement that every old controller using the shared database has stopped; it
is not a process-liveness check. Migration reads SQLite's committed logical state,
preserves the shared source as a backup, validates selected identities and values,
and publishes the rows, owner, and completion receipt in one destination commit.
The durable receipt makes later readiness checks and migration commands idempotent,
including after migrated obligations have completed and been deleted.

Ownership refusals identify both the configured and obligation repositories through
the normal Loguru sinks. An unbound scheduler may exist during engine construction,
but cannot enter its serving loop; production installs the immutable binding and all
handlers before starting recovery.

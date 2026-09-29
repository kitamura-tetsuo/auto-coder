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

## Production adoption and operator workflow

Every production pending-work access resolves its store from an explicit repository:
the engine/invocation target or the identity of the obligation being processed.
`get_pending_work_store(repository)` returns the READY store for that repository, cached
by its repository-specific destination path so constructing or using another repository
can never redirect it. There is no no-argument, current-directory, `REPO_NAME`, or
last-used-repository fallback. A store opened this way never creates or repairs its
database and re-verifies the durable owner and initialization record on every use.

`AutomationEngine.start_automation`, `run`, and `process_single` (including `--only`)
establish READY before any dependent work. When legacy rows owned by the repository
exist without a completed destination, or storage is unavailable or conflicting, they
refuse, keep all retained state, and report the repository, storage locations, and
`auto-coder pending-work migrate --repository OWNER/REPO --offline` through the normal
loguru sinks. Startup reconciliation, Issue/PR evaluation deferrals, Codex retry
handoffs, specification/decomposition publication receipts, and repair-allowance grants
(`pr-repair resume` and durable reevaluation reconciliation, filtered to the running
repository) all use that same store. A grant is refused before it is recorded when the
repository is not READY.

Shared GitHub request admission is unchanged: the governor database is not partitioned.

Commands:

* `auto-coder pending-work list [--repository OWNER/REPO]` is a read-only aggregate. Each
  obligation line keeps the existing JSON fields and adds `storage_path` and
  `storage_state` (`initialized` or `migration-required`). Legacy rows of a repository
  with a valid completed destination are never shown. It never initializes, imports, or
  resets anything; inaccessible or inconsistent storage is reported on stderr with a
  nonzero exit.
* `auto-coder pending-work retry --repository R --entity E --stage S [--revision V]`
  resolves only R's READY destination and resets exactly one retained identity to
  waiting (deadline now, throttle count zero), keeping its reason, last error, and
  unfinished-effect receipts. No match, an ambiguous match, migration-required, or
  unavailable storage exits nonzero without changing anything. It never executes work.

Upgrade: stop every legacy controller sharing the old database, run the offline
migration for each repository that needs it, start the upgraded controllers, inspect with
`pending-work list`, and use targeted `retry` only for a selected retained block. The old
database is preserved but is not the live queue after cutover; running old and upgraded
writers together is unsupported.

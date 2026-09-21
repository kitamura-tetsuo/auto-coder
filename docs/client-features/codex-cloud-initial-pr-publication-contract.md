# Codex Cloud initial PR publication contract

Every initial Codex Cloud Issue route uses a provider-specific prompt carrying
the complete current Issue, canonical Issue URL, attempt, configured backend,
and actual base branch. The task must implement and validate the Issue, publish
from a unique task branch (never the shared `work` branch), include an explicit
closing reference, and verify the real GitHub PR. Prepared text or a local
commit is not publication; inability to publish must be reported with the
observed blocker, while Auto-Coder continues to treat its independent GitHub
observation as authoritative. Other providers and existing-PR repair prompts
retain their separate ownership contracts.

Before task creation, the route durably records a replay-stable, unique expected
head repository and ref with the accepted launch identity. PR attribution is a
separate, conflict-detecting durable record: it requires an accepted run, an
exact source-Issue closing relationship, and either a supported exact task URL
in fresh PR metadata or the exact retained publication head. It never follows
the mutable Issue current-task pointer, and exposes verified, unresolved,
unavailable, and conflict outcomes plus a repository consistency revision.

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

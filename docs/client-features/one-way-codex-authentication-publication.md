# One-way Codex authentication publication

An optional `[github_secrets]` table in `config.toml` publishes the exact local
Codex login file (`$CODEX_HOME/auth.json`, or `~/.codex/auth.json`) to a
repository Actions secret. Publication is disabled by default. Enabling it
requires a dedicated `token` and explicit `repository = "owner/name"`; the
optional `secret_name` defaults to `CODEX_AUTH_JSON`. This credential is used
only by the Actions Secrets publisher and is never shared with ordinary GitHub
Issue, PR, repository, or workflow operations. Invalid or unavailable local
JSON is never published, remote secret values are never read, and publication
failures do not stop normal processing. A later run republishes the complete
current file, allowing refreshed local login state to replace stale CI state.

```toml
[github_secrets]
enabled = true
token = "a-dedicated-secrets-write-token"
repository = "owner/name"
secret_name = "CODEX_AUTH_JSON"
```

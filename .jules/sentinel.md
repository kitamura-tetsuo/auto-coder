## 2025-02-18 - Hardcoded Neo4j Credentials
**Vulnerability:** The GraphRAG integration used a hardcoded "password" for Neo4j in `docker-compose.graphrag.yml`, `cli_helpers.py`, and `cli_commands_graphrag.py`. This credential was also written to a local `.env` file for the MCP server.
**Learning:** Default credentials in development tools can be dangerous if the tool is deployed or used in shared environments. Hardcoding them in multiple places makes it difficult for users to secure their setup.
**Prevention:** Use environment variables for sensitive credentials with sensible defaults (if necessary for dev experience), but always allow overrides. Use `${VAR:-default}` in docker-compose and `os.getenv` in Python.

## 2025-05-23 - Sensitive Data Exposure in Logs
**Vulnerability:** `GHCommandLogger` logged command arguments to CSV files without redaction, potentially exposing secrets passed as arguments (e.g., to `gh secret set` or `gemini config`).
**Learning:** Logging command execution is useful for debugging but dangerous if not sanitized. CLI tools often pass secrets as arguments.
**Prevention:** Implement redaction logic in logging utilities to mask known secret patterns (tokens, keys) before writing to persistent logs.

## 2025-02-18 - Hardcoded Neo4j Credentials
**Vulnerability:** The GraphRAG integration used a hardcoded "password" for Neo4j in `docker-compose.graphrag.yml`, `cli_helpers.py`, and `cli_commands_graphrag.py`. This credential was also written to a local `.env` file for the MCP server.
**Learning:** Default credentials in development tools can be dangerous if the tool is deployed or used in shared environments. Hardcoding them in multiple places makes it difficult for users to secure their setup.
**Prevention:** Use environment variables for sensitive credentials with sensible defaults (if necessary for dev experience), but always allow overrides. Use `${VAR:-default}` in docker-compose and `os.getenv` in Python.

## 2025-05-23 - Sensitive Data Exposure in Logs
**Vulnerability:** `GHCommandLogger` logged command arguments to CSV files without redaction, potentially exposing secrets passed as arguments (e.g., to `gh secret set` or `gemini config`).
**Learning:** Logging command execution is useful for debugging but dangerous if not sanitized. CLI tools often pass secrets as arguments.
**Prevention:** Implement redaction logic in logging utilities to mask known secret patterns (tokens, keys) before writing to persistent logs.

## 2025-12-18 - Insecure File Permissions for Generated .env Files
**Vulnerability:** The application generated `.env` files containing sensitive credentials (like `NEO4J_PASSWORD`) using standard `open()`, which creates files with default permissions (often world-readable). `os.chmod` was sometimes called afterwards, leaving a race condition window.
**Learning:** Creating sensitive files with default permissions and relying on a subsequent `chmod` is insecure due to race conditions.
**Prevention:** Use `os.open` with `os.O_CREAT | os.O_WRONLY | os.O_TRUNC` and `mode=0o600`, then wrap the file descriptor with `os.fdopen`. This ensures the file is created with restricted permissions atomically.

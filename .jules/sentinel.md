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

## 2025-05-22 - Docker Compose Sudo Environment Stripping
**Vulnerability:** GraphRAGDockerManager used `sudo` to retry failed docker commands (due to permission errors) but failed to preserve the `NEO4J_PASSWORD` environment variable. This caused `docker-compose` to silently fall back to the default weak password ("password") even when the user had set a secure password.
**Learning:** `sudo` strips environment variables by default for security, but this can lead to silent security downgrades when applications rely on environment variables for configuration/secrets.
**Prevention:** When wrapping commands with `sudo`, explicitly preserve critical security environment variables using `--preserve-env=VAR` or `sudo -E` (if appropriate), or ensure the child process receives the configuration via another channel (e.g., config file).

## 2025-12-21 - Command Execution Logging Leak
**Vulnerability:** `CommandExecutor` logged all executed commands and their output for debugging purposes, but lacked the redaction logic present in `GHCommandLogger`. This could expose secrets (like API keys or tokens) passed as command arguments or returned in stdout/stderr.
**Learning:** Centralized logging utilities must consistently apply security sanitization. Using different logging paths for different command types (e.g., specific `gh` logger vs generic executor) can lead to inconsistent security coverage.
**Prevention:** Centralize redaction logic in a shared utility and apply it to all command execution logging points, regardless of the command type.

## 2026-01-20 - Insecure File Permissions for Config Files
**Vulnerability:** `llm_config.toml` containing API keys was created with default permissions (often world-readable).
**Learning:** Any file that might contain secrets (like config files) must be created with restricted permissions from the start. Race conditions in `open()` then `chmod()` are a risk.
**Prevention:** Use the same `os.open` with `0o600` pattern for all configuration files that might store sensitive data.

## 2026-02-13 - Insecure Test Log File Permissions
**Vulnerability:** Test logs containing potentially sensitive stdout/stderr were created with default permissions (often world-readable).
**Learning:** Even diagnostic logs can contain sensitive information captured from test runs.
**Prevention:** Apply strict file permissions (0o600) to all log files that store captured command output using `os.open` and `os.fdopen`.

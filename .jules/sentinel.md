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

## 2026-01-21 - Unredacted Function Arguments in Logs
**Vulnerability:** The `@log_calls` decorator logged function arguments and return values using `repr()` without any redaction, exposing sensitive data (like tokens passed to backend clients) in debug logs.
**Learning:** Decorators used for tracing or debugging must implement the same security sanitization as explicit logging calls. It's easy to overlook "internal" tracing tools as sources of leakage.
**Prevention:** Apply `security_utils.redact_string` to all logged arguments and return values in tracing decorators.

## 2026-01-22 - Insecure Log File Permissions
**Vulnerability:** `LogEntry.save` in `src/auto_coder/log_utils.py` used standard `open()`, creating log files with default permissions (often `0o664` or `0o644`), potentially exposing sensitive information captured in logs to other users on the system.
**Learning:** Even "temporary" or "log" files can contain sensitive data from the runtime environment. Default `open()` behavior is insufficient for security-critical applications in multi-user environments.
**Prevention:** Use `os.open` with `os.O_CREAT | os.O_WRONLY | os.O_TRUNC` and `mode=0o600` for all log file creation, ensuring restricted access from the moment of creation.

## 2026-02-14 - Insecure Permissions on Existing Log Files
**Vulnerability:** `LogEntry.save` and `save_commit_failure_history` used `os.open` with `O_CREAT` and `0o600` permissions, which correctly secures *new* files but fails to restrict permissions on *existing* files (which retain their original, often world-readable permissions).
**Learning:** `os.open`'s mode argument only applies when a file is created. Overwriting a file with `O_TRUNC` does not change its permissions.
**Prevention:** Always call `os.chmod(path, 0o600)` in addition to using `os.open` with restricted permissions, to ensure that pre-existing files are also secured.

## 2026-02-18 - Insecure Webhook Signature Verification
**Vulnerability:** `verify_github_signature` parsed the signature header (splitting by `=`) before verifying it, leading to potential `ValueError` crashes (500 errors) on malformed input and timing information leakage regarding the signature format.
**Learning:** Validating complex string formats before cryptographic verification can introduce parsing errors and information leaks.
**Prevention:** Construct the *entire* expected string (e.g. `sha256=<digest>`) and use `hmac.compare_digest` on the full string to avoid parsing steps and ensure constant-time comparison.

## 2026-02-18 - CI Failures from Formatting
**Vulnerability:** Not a security vulnerability, but a process failure. The CI pipeline blocked the security fix because the new test file `tests/test_webhook_security.py` violated the project's formatting rules (Black).
**Learning:** Security fixes must adhere to code style guidelines to be deployable. A secure patch that breaks the build cannot protect users.
**Prevention:** Always run the project's formatter (e.g., `uv run black`) on new test files before submission.

## 2026-02-18 - CI Failures from Import Sorting
**Vulnerability:** Process failure. The CI pipeline blocked the security fix again because `isort` failed on the new test file `tests/test_webhook_security.py`, even though `black` passed.
**Learning:** Formatting checks often include both code style (Black) and import sorting (isort). Running one without the other is insufficient.
**Prevention:** Always run the full linting suite (e.g., `scripts/test.sh` or explicitly `uv run black . && uv run isort .`) before submission.

## 2026-02-18 - CI Failure from Global Side Effects in Tests
**Vulnerability:** Process failure.  globally mocked  in  at the top level. This caused other tests (like ) to fail with  or  because  was replaced with a Mock object that didn't support sub-imports (like ) during test collection.
**Learning:** Avoid top-level  patching in test files. It pollutes the global namespace and breaks other tests in unpredictable ways depending on execution order.
**Prevention:** Use  context managers or  fixtures to mock modules temporarily for specific tests, or ensure mocks are robust enough to support required imports if absolutely necessary. Ideally, don't mock 3rd party libraries globally; use their provided test utilities or mock specific imports in the *code under test* using .

## 2026-02-18 - CI Failure from Global Side Effects in Tests
**Vulnerability:** Process failure. tests/test_webhook_delay.py globally mocked fastapi in sys.modules at the top level. This caused other tests (like test_webhook_security.py) to fail with ModuleNotFoundError or ImportError because fastapi was replaced with a Mock object that didn't support sub-imports (like fastapi.testclient) during test collection.
**Learning:** Avoid top-level sys.modules patching in test files. It pollutes the global namespace and breaks other tests in unpredictable ways depending on execution order.
**Prevention:** Use unittest.mock.patch.dict context managers or pytest fixtures to mock modules temporarily for specific tests.

## 2026-02-18 - CI Failure from Global Side Effects in Tests
**Vulnerability:** Process failure. tests/test_webhook_delay.py globally mocked fastapi in sys.modules at the top level. This caused other tests (like test_webhook_security.py) to fail with ModuleNotFoundError or ImportError because fastapi was replaced with a Mock object that didn't support sub-imports (like fastapi.testclient) during test collection.
**Learning:** Avoid top-level sys.modules patching in test files. It pollutes the global namespace and breaks other tests in unpredictable ways depending on execution order.
**Prevention:** Use unittest.mock.patch.dict context managers or pytest fixtures to mock modules temporarily for specific tests, or ensure mocks are robust enough to support required imports if absolutely necessary. Ideally, don't mock 3rd party libraries globally; use their provided test utilities or mock specific imports in the *code under test* using patch.

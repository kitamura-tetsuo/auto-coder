## 2026-01-20 - [CRITICAL] Exposed Sensitive Data in Logs via Decorator
**Vulnerability:** The `@log_calls` decorator in `src/auto_coder/logger_config.py` was logging function arguments and return values using `repr()` without any redaction. This decorator was used on functions handling LLM prompts and responses (`backend_manager.py`), potentially leaking source code, API keys, or PII contained in the prompts or generated code into debug logs.
**Learning:** Generic logging decorators must be security-aware. When logging "all calls" or "all arguments" for debugging, there is a high risk of capturing sensitive data passing through the system. Standard `repr()` is not safe for logging in security-sensitive applications.
**Prevention:**
1. Avoid using generic "log all arguments" decorators on functions handling sensitive data.
2. If used, ensure the logging mechanism integrates with a centralized redaction utility (like `redact_string`).
3. Apply redaction to both input arguments and return values.

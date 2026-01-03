## 2024-05-24 - Secure File Creation Race Conditions
**Vulnerability:** A TOCTOU (Time-of-Check to Time-of-Use) race condition existed in `BackendStateManager.save_state` where a file was created with default permissions (often 644) using `open()`, and then `os.chmod()` was called to restrict them to 0o600. This left a small window where the file was world-readable.
**Learning:** Even internal state files that seem low-risk should be created securely. The pattern of `open() -> chmod()` is insecure for sensitive files.
**Prevention:** Always use `os.open` with `os.O_CREAT | os.O_WRONLY | os.O_TRUNC` and mode `0o600` to create files atomically with restricted permissions. Then wrap the file descriptor with `os.fdopen`.

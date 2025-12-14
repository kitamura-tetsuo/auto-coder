# Sentinel's Security Journal

## 2025-12-13 - Insecure File Permissions on Generated Secrets
**Vulnerability:** The `graphrag setup-mcp` command generated a `.env` file containing sensitive credentials (Neo4j password) with default file permissions (typically 0644), making it readable by other users on the system.
**Learning:** Python's `open()` creates files with default permissions based on the system `umask`. When writing sensitive data, explicit permission handling is required.
**Prevention:** Always set file permissions to `0o600` (owner read/write only) immediately after creating files containing secrets, or use `os.open` with `O_CREAT` and `mode=0o600`.

## 2025-12-14 - Accidental Commit of Local Configuration
**Vulnerability:** A local configuration file `src/auto_coder/~/.claude/settings.json` containing a live JWT token was accidentally committed to the repository. The unusual path incorporating `~` suggests a mistake in command execution or file tracking.
**Learning:** Files and directories with names starting with `~` are not automatically expanded by Git or some tools, leading to literal paths that mimic home directories being committed.
**Prevention:** Ensure `.gitignore` includes patterns for local configuration directories like `.claude/`, `.env`, etc., and verify file lists before committing.

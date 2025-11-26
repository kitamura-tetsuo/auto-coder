# Auto-Coder Agent Guidelines

## Project Overview

This project is a Python application that automates application development using an AI CLI backend (default: `codex`, switchable to `gemini` or `qwen` via `--backend`).
It retrieves issues and error-related PRs from GitHub to build and fix the application, and automatically creates feature-addition issues when necessary.

## Development Guidelines

### Code Quality

* Create corresponding tests for all implemented features.
* Define strict expectations for test assertions.
* When a test fails, first verify that the expected values align with the specifications.
* Do not skip tests.
* Run all tests at the end of tasks.
* Do not copy implementation code into test files for testing.
* Do not use mocks in end-to-end (e2e) tests.
* Run e2e tests in headless mode.
* Do not return Dict[str, Any]. Use @dataclass with initial values instead.
* Write comments and messages in English.
* Remove backwards compatibility code and unused code and dependencies.

### Project Structure

* Maintain a standard Python project structure.
* Document all features in `docs/client-features.yaml` as soon as possible.
* Read `docs/client-features.yaml` to comply with specifications and not to degrade.
* Do not create duplicate functions in multiple locations.

### GitHub Operations

* Use the `gh` CLI command for all GitHub operations.
* Use the GitHub API properly to retrieve issues and PRs.

### Dependency Management

* Use `uv` for dependency management.

### Logging Configuration

* Use the `loguru` library for logging.
* Include the filename, function name, and line number in log entries.
* Display colored logs for console output for readability.
* Save logs to files with rotation enabled.

### CI/PR Checks

* Make PR checks via GitHub Actions mandatory.
* Workflow files:
  * `.github/workflows/pr-tests.yml` (name: `PR Tests`)
  * `.github/workflows/update-version.yml` (name: `Update Version`)
* Required jobs in `PR Tests`:
  * **Lint & Type Check** (black / isort / flake8 / mypy)
  * **Tests with Coverage** (pytest with coverage reports)
  * Target Python version: 3.11
* Branch protection should include the following required status checks:
  * `PR Tests / Lint & Type Check`
  * `PR Tests / Tests with Coverage`
  * `Update Version / update-version` (for main branch)

### LLM Execution Policy (Important)

## Specification Notes (Operational Key Points)

### Backend Configuration System Update

* **Client Initialization Changes:** LLM clients (CodexClient, ClaudeClient, GeminiClient, QwenClient, AuggieClient) now accept `backend_name` parameter during initialization instead of direct `model_name` parameter.
* **Configuration-Based Model Selection:** The client classes now retrieve model names and other configuration values from the backend configuration system rather than accepting them directly.
* **Backward Compatibility:** The system continues to support default configurations while allowing for more flexible backend-specific configurations.
* **Test Adaptations:** Unit tests have been updated to reflect the new initialization pattern, using mocked configurations to test different backend scenarios.

### Backend State Persistence and Auto-Reset

* **Backend State Persistence:** The system persists the current backend selection across application restarts by saving state to `~/.auto-coder/backend_state.json`.
* **Auto-Reset Behavior:** After 2 hours (7200 seconds) of being on a non-default backend, the system automatically resets to the default backend to prevent getting stuck on a specific backend for extended periods.
* **State Schema:** The state file contains:
  - `current_backend`: The name of the currently active backend
  - `last_switch_timestamp`: Unix timestamp of when the backend was last switched
* **Thread Safety:** Backend state operations are thread-safe using locks.
* **Automatic Sync:** On startup, the system checks the saved state and either:
  - Resets to default backend if > 2 hours have passed and current backend ≠ default
  - Syncs to the saved backend if < 2 hours have passed
  - Starts with default backend if no state file exists

* **PR Handling:** If PR checks fail, the default behavior skips merging from the base branch and proceeds directly to fixing (`--skip-main-update`).
  To revert to the old behavior, specify `--no-skip-main-update`.
* **Analysis Phase Prohibited:** Do not call LLMs solely for analysis (e.g., `analyze_issue`).
* **Single Execution Rule:** Each issue/PR must invoke the LLM only once, covering detection, implementation, testing, and PR update in a single run.
* **No Split Execution:** Do not divide a single task into multiple LLM calls (this does not improve accuracy).
* **Exceptions:** Non-LLM operations (Git/GitHub API, build, test, static analysis, etc.) are allowed as needed. Automatic backend switching is permitted only within the same LLM run.
* **Implementation Note:** Do not add or use methods like `analyze_issue` in clients such as `CodexClient`. If such calls exist in the code, remove them and unify under the single-execution flow.
* **PR Output Policy:**
  LLMs must not post comments on PRs. They should only perform minimal code modifications, `git add/commit/push`, and `gh pr merge` if conditions are met.
  No review or comment text output is allowed.
  On success, output only a single line beginning with `ACTION_SUMMARY:`.
  If the issue cannot be fixed, output `CANNOT_FIX`.
* **TEST_SCRIPT_PATH (`scripts/test.sh`) Policy:**
  * The `scripts/test.sh` used during execution is the one in the *target repository*. Optimizing the one in this repository has no effect.
  * Automated routines (`run_local_tests`, `run_pr_tests`, etc.) must never call `pytest` directly—they must always invoke `$TEST_SCRIPT_PATH`.
    Even for single-test reruns, call `bash $TEST_SCRIPT_PATH <file>`.
  * Check for the existence of `TEST_SCRIPT_PATH` only *once at startup*.
    If missing, immediately terminate with an error.
    No fallback checks should occur afterward.
  * The `scripts/test.sh` script now supports:
    - Preferred uv runner for consistent, reproducible environments
    - Fallback to system Python's pytest when uv is not available
    - Optional local virtualenv activation via AC_USE_LOCAL_VENV=1
    - Always enables auto-syncing dependencies with uv

### Git Commit/Push Policy (English)
* Centralize all `git commit` and `git push` operations through dedicated helper routines.
* Do not directly invoke `git commit` or `git push` across the codebase.
* **Rationale:** Scattered commit/push logic leads to duplicate behavior, inconsistent error handling, and subtle bugs (e.g., missing unified handling for formatter hooks like `dprint`).
* **Implementation:**
  * `git_utils.git_commit_with_retry(commit_message, cwd=None, max_retries=1)`
    → Centralized commit helper that automatically detects `dprint` formatting errors, runs `npx dprint fmt`, stages changes, and retries once.
  * `git_utils.git_push(cwd=None, remote='origin', branch=None, commit_message=None)`
    → Centralized push helper with consistent error handling. Detects `dprint` formatting errors in push hooks, runs `npx dprint fmt`, stages all changes, re-commits, and retries push.
  * All commit/push operations must use these helpers.
  * Direct invocations of `git commit` or `git push` via `CommandExecutor` are strictly prohibited outside these helpers.

### GraphRAG MCP Auto-Setup

* During normal command execution (`process-issues`, `create-feature-issues`, `fix-to-pass-tests`), the setup and startup of `graphrag_mcp` are automated.
* The `initialize_graphrag()` function is invoked at startup to:
  * Check if the `~/graphrag_mcp` directory exists.
  * If missing, automatically run `run_graphrag_setup_mcp_programmatically(silent=True)`.
  * Start the Docker container and update the index.
  * Launch the MCP server.
* The `check_graphrag_mcp_for_backends()` function verifies and adds configurations for each backend (server assumed already installed).
* Implementation file: `src/auto_coder/cli_helpers.py`.

### MCP-PDB Setup Support

* Add CLI group `auto-coder mcp-pdb` with:
  * `print-config --target [windsurf|claude]`: outputs configuration snippets.
  * `status`: checks for prerequisite commands (e.g., `uv`) and displays setup hints.
* Does not perform actual installation—only assists with configuration for the user’s local environment (Windsurf/Claude).

## Main Features

* Retrieve issues and PRs via the GitHub API (sorted by oldest first).
* **Jules Mode (optional):** adds the `jules` label to issues; PRs are handled by the usual AI backend (default: `codex`).
* **Normal Mode (default):** single-run automation using `codex` or the backend specified via `--backend` (`Gemini` or `Qwen`); analysis-only calls are prohibited.
* **Automatic Model Switching:** automatically switches to `gemini-2.5-flash` for fast conflict resolution during PR merges.
* **Special Handling for Package-Lock Conflicts:** automatically deletes and regenerates lockfiles (`package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`) to resolve conflicts.
* **Automatic Resolution of Dependency-Only `package.json` Conflicts:** if only dependency sections differ, automatically merges preferring newer or larger dependency sets.
* **Gemini Prompt Escaping:** automatically escapes `@` as `\@` in prompts to safely pass to the Gemini CLI.
* Automatic detection of missing features and issue creation.
* Automated code fixes and builds.
* PR prioritization (merge if GitHub Actions pass and PR is mergeable; otherwise, fix).
* Introduce LLM skip flag: when automatic conflict resolution or push completes (e.g., `package-lock.json` merge), skip subsequent LLM analysis explicitly.
* Jules Mode is ON by default: toggle via `--jules-mode` / `--no-jules-mode` (default ON).
* **Codex-MCP Mode:**
  During single PR processing or local error fixing, maintain a persistent `codex mcp` session.
  Minimal JSON-RPC (`initialize` / `echo` tool calls) implemented; advanced operations handled via `codex exec`.

## Test Strategy

* **Unit Tests:** test each module’s individual functionality.
* **Integration Tests:** test API and CLI integrations.
* **End-to-End Tests:** test full automation flows.

---

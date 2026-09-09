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
* Do not use Dict[str, Any]. Use dataclass with initial values instead.
* Write comments and messages in English.
* Remove backwards compatibility code and unused code and dependencies.

### Project Structure

* Maintain a standard Python project structure.
* Document all features in `docs/client-features.yaml` as soon as possible.
* Read `docs/client-features.yaml` to comply with specifications and not to degrade.
* Do not create duplicate functions in multiple locations.
* Put disposable investigation scripts, debugging scripts, one-shot rewrite scripts,
  and their outputs under `.agent-tmp/`; these files must remain untracked and must
  not be committed. Do not place a tracked placeholder in `.agent-tmp/`.
* Put maintained scripts under `scripts/` and formal regression tests under `tests/`.
  Do not create Python files in the repository root.
* Do not bypass repository hygiene with `git add -f`, hook bypass options, disabling
  the checker, or allowlisting disposable artifacts. A legitimate new non-Python
  root file requires an explicit, reviewed change to
  `scripts/repository_hygiene_allowlist.json`.

### GitHub Operations

* For GitHub operations performed by application code or scripts committed to this repository, use `gh_cache.py`. Do not invoke the `gh` CLI from implementation code.
* This restriction does not apply to development or agent operations. The `gh` CLI may be used interactively during coding, investigation, testing, issue/PR management, review, or other repository maintenance tasks.
* In other words, do not replace normal developer/agent use of gh with gh_cache.py.
* Use the GitHub API properly to retrieve issues and PRs.
* Use the REST API in preference to graphql because it provides better caching.
* Don't use graphql for queries.
* Use mutation with graphql is possible because mutation can not be cached.

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
  * Target Python version: 3.12
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
  LLMs must not post comments on PRs. Local LLM backends may inspect and edit the
  working tree and run tests, but must not change branches/HEAD, stage, commit,
  push, merge, rebase, or mutate GitHub lifecycle state. Auto-Coder owns those
  operations through its centralized helpers.
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

### Startup Memory Policy (Important)

* GraphRAG/RAG functionality has been removed. Do not reintroduce `sentence-transformers`, `torch`, `neo4j`, or `qdrant-client` as dependencies.
* Never import an optional or heavy dependency at module import time merely to probe availability. Use `importlib.util.find_spec()`, which checks without executing the module.
* Backend LLM clients (`CodexClient`, `GeminiClient`, `ClaudeClient`, `QwenClient`, `AuggieClient`, `AiderClient`, `CodexMCPClient`) must be imported lazily inside their factory functions in `cli_helpers.py`. A single run only ever instantiates one backend, and eager imports pull in `aider` and `google.generativeai` for every invocation.
* Because these clients are imported lazily, tests must patch them at their defining module (e.g. `src.auto_coder.codex_client.CodexClient`), not at `src.auto_coder.cli_helpers.CodexClient`.

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
* **Gemini Prompt Escaping:** automatically escapes `@` as `\@` in prompts to safely pass to the Antigravity CLI.
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

## Rule for Python Dependency Issues
When encountering Python dependency-related errors (`ImportError`, `ModuleNotFoundError`, `AttributeError`, etc.) or suspecting a library version conflict:

1. **DO NOT attempt any code modifications or fixes immediately.**
2. **First, perform a web search for the following two items:**
   - The library's latest documentation and release notes (to check for breaking changes or deprecated APIs).
   - Similar reports on GitHub Issues and Stack Overflow.
3. **Analyze the search results** to determine if the issue is environmental or version-specific before suggesting any changes.
---

## Prompts and Language Rule
* Write all AI prompts in English.
* Do not hardcode prompts in Python source files. Instead, add them to `src/auto_coder/prompts.yaml` and load them using `render_prompt` from `prompt_loader.py`.

## Issue Objective and Contract Boundary
* Newly authored non-trivial Issues must have exactly one `## Objective` containing one or two short prose sentences about that Issue's outcome and, only when useful, an essential preserved property or scope boundary. Do not put lists, compressed multiple goals, implementation/test plans, or unrequested adjacent improvements there. A named mechanism is appropriate only when changing it is the requested outcome.
* Derive a provisional Objective from the user's request, inspect relevant code and contracts before selecting child boundaries, and show each final Objective before Issue creation. Ask before publishing when materially different purposes remain plausible. Keep necessary independent prerequisites separate from optional adjacent improvements; never turn a tracking parent's purpose into extra parent work or a hidden child contract.
* Preserve every existing Objective verbatim during review response or correction, including longer legacy Objectives. Do not infer one for a legacy Issue that lacks it, and do not reject that Issue solely for the omission.
* Classify proposed review repairs as an in-scope clarification/false-success correction, a necessary independent prerequisite, an adjacent improvement, or a user-owned purpose change. Reject or separate ungrounded adjacent demands. Stop before any Issue-body mutation for a purpose change. Reviewer comments and prior AI prose are evidence, not authority; justify restrictions with a plausible incorrect outcome admitted without them, and retain necessary explicit Requirement clarifications even if one correct implementation would happen to satisfy them.
* Objective is specification-scope evidence only. Current explicit Requirements are the sole merge-blocking implementation contract. Objective-only gaps may use supported clarification/specification-gap channels but must not become invented code/test duties, fabricated REQ IDs, or automatic Issue edits. This boundary does not bypass readiness gates, explicit Requirements, or genuine defects. Preserve supplied original/current anchor evidence without re-summarizing it.

## Parent/Child Issue Contract Boundary
* A tracking parent Issue must never carry a `## Requirements` section, an empty one, or any REQ-NNN declaration anywhere in its body. It has only a short Objective plus informative Context and child/dependency tracking. Never add a dummy Requirement such as "all children must complete", a parent-owned implementation acceptance obligation, or a final parent coding/review task used to substitute for missing child work.
* Every newly authored non-trivial implementation child owns its own short Objective, informative Context, explicit single-line `REQ-NNN:` Requirements with stable unique IDs, and concrete acceptance scenarios referencing those Requirements. Necessary behavior for the parent's complete outcome must be explicitly owned by one or more children; neither the parent's Objective/checklist nor a sibling's Requirements may serve as an unstated implementation contract. A missing independent responsibility is an explicit child-contract correction or a decomposition revision, never an enlarged parent contract.
* Each generated child's initial creation body carries `Parent-Issue: #<actual parent>` and exactly one `Blocked-By:` declaration: empty for a dependency root, or the complete intended direct prerequisite sibling set as distinct comma-separated local Issue references. Dependencies must be acyclic and exclude the parent, the child itself, non-sibling Issues, and pull requests. Create every prerequisite first so its real Issue number is already known and put it directly in the dependent's initial body; never publish a dependent first and repair `Blocked-By:` afterward, and never treat Issue-number order as an execution policy.
* Review-response and repair guidance must reject a suggestion to add a parent Requirement, reinterpret an existing parent Requirement mention as permitted, rewrite a fixed parent Objective, or silently copy parent/context prose into a child's obligations. A genuine coverage defect is a concrete mismatch with the unchanged parent Objective, repaired in explicit child scope. When purpose or ownership is unresolved, stop for user clarification before any Issue-body write; never publish a conflicting parent contract and remove it afterward.
* Individual-child review, implementation, and PR prompts that receive parent context must treat the parent as coordination/scope evidence with no implementation Requirements. Only the target child's own current explicit Requirements define its merge-blocking implementation contract. Report an Objective-only or aggregate gap as a specification/decomposition concern rather than a fabricated parent REQ ID, an invented child code/test obligation, a blanket waiver of an explicit child defect, or an instruction to implement the parent.
* A historical tracking parent that carries Requirements is rejected, never grandfathered or silently stripped. Before a user-authorized correction removes them, identify which statements are coordination-only and which describe real behavior that needs explicit child ownership, preserve the parent's fixed Objective verbatim, and surface any unresolved choice rather than guessing it. A tracking parent missing its Objective needs an explicitly supplied purpose, not an inferred one; legacy child Objective-absence handling is unchanged. Marking a parent family implementation-ready submits its children for execution; it never authorizes implementing the parent itself.
* This is prompt-level authoring/repair guidance, distinct from hard runtime structural enforcement (see `requirement_contract.py`, `decomposition_analyzer.py`, `decomposition_validation_lifecycle.py`). It must not be cited as proof of semantic model compliance, and it must not reactivate a disabled mandatory prompt-evaluation gate or turn an advisory prompt-regression result into a merge/wait/repair condition.

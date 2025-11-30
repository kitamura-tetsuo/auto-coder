# Auto-Coder

A Python application that automates application development using an AI CLI backend. It retrieves issues and error-related PRs from GitHub to build and fix the application, and automatically creates feature-addition issues when necessary.

## Features

### 🔧 Core Features
- **GitHub API Integration**: Automatic retrieval and management of issues and PRs
- **AI Analysis (multiple backends configurable via configuration file)**: Automatic analysis of issue and PR content
- **Automated Processing**: Automatic actions based on analysis results
- **Feature Proposals**: Automatic proposal of new features from repository analysis
- **Report Generation**: Detailed reports of processing results
- **PR Mergeability Handling**: Automated detection and remediation of non-mergeable PRs through base branch updates and intelligent conflict resolution

### 🚀 Automated Workflow
1. **Issue Processing**: Retrieve open issues and analyze with Gemini AI
2. **PR Processing**: Retrieve open PRs and evaluate risk levels
3. **Feature Proposals**: Propose new features from repository context
4. **Automatic Actions**: Add comments or auto-close based on analysis results

### 🔄 PR Mergeability Handling

Auto-Coder includes intelligent handling for PRs that are not immediately mergeable. When a PR is detected as non-mergeable, the system can automatically:

1. **Detect and Analyze**: Checks PR mergeability status and merge state (CLEAN, DIRTY, UNKNOWN)
2. **Checkout PR Branch**: Switches to the PR branch for local processing
3. **Update from Base**: Fetches and merges the latest changes from the base branch
4. **Resolve Conflicts**: Uses specialized handlers for different conflict types:
   - **Package-lock conflicts**: Automatically deletes and regenerates lock files (package-lock.json, yarn.lock, pnpm-lock.yaml)
   - **Package.json dependency conflicts**: Intelligently merges dependency sections, preferring newer versions
   - **General conflicts**: Uses LLM to resolve remaining conflicts with context
5. **Push Changes**: Commits and pushes the updated branch with automatic retry logic
6. **Signal Completion**: Sets `ACTION_FLAG:SKIP_ANALYSIS` marker after successful remediation

This feature is controlled by the `ENABLE_MERGEABILITY_REMEDIATION` configuration flag. When enabled, non-mergeable PRs are automatically remediated without manual intervention.

**Related Configuration:**
- `ENABLE_MERGEABILITY_REMEDIATION` (default: false) - Enables automatic remediation
- `SKIP_MAIN_UPDATE_WHEN_CHECKS_FAIL` (default: true) - Controls base branch updates during fix flows

See [docs/client-features.yaml](docs/client-features.yaml) for complete technical documentation.

### 📊 Logging and Monitoring
- **Structured JSON Logs**: All LLM interactions are logged in JSON Lines format at `~/.auto-coder/logs/llm_output.jsonl`
- **User-Friendly Output**: Execution summaries are printed to console for immediate feedback
- **Rich Metadata**: Each log entry includes timestamp, backend, model, prompt/response lengths, duration, and status
- **Environment Control**: Toggle logging via `AUTO_CODER_LLM_OUTPUT_LOG_ENABLED` environment variable
- **Error Tracking**: Detailed error information for failed requests
- **Easy Analysis**: Machine-readable format for parsing with `jq` or custom scripts

## Installation

### Prerequisites
- Python 3.9 or higher
- [gh CLI](https://cli.github.com/) pre-authenticated (`gh auth login`)
- [Codex CLI](https://github.com/openai/codex) installed (default backend)
- [Gemini CLI](https://ai.google.dev/gemini-api/docs/cli?hl=en) required when using Gemini backend (`gemini login`)

### Setup

1. Clone the repository:
```bash
git clone https://github.com/your-username/auto-coder.git
cd auto-coder
```

curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync

2. Install dependencies and make it executable from any directory:
```bash
source ./venv/bin/activate
pip install -e .
# Or install directly without cloning the repository
pip install git+https://github.com/your-username/auto-coder.git
```

> Note (PEP 668 avoidance/recommended): In environments where system Python is externally managed and `pip install` is blocked, we recommend installation via pipx.
>
> Example for Debian/Ubuntu:
>
> ```bash
> sudo apt update && sudo apt install -y pipx
> pipx ensurepath   # Restart/re-login to shell if needed
> pipx install git+https://github.com/kitamura-tetsuo/auto-coder.git
> auto-coder --help
> ```


3. Create configuration file if needed:
```bash
cp .env.example .env
# Tokens can be left blank as gh and gemini authentication information is used automatically
```

## Usage

### CLI Commands

#### `process-issues`

Process GitHub issues and PRs using AI CLI.

```bash
auto-coder process-issues [OPTIONS]
```

**Options:**

- `--repo TEXT`: GitHub repository (owner/repo). Auto-detected if not specified.
- `--github-token TEXT`: GitHub API token.
- `--jules-mode / --no-jules-mode`: Run in jules mode (default: on).
- `--disable-labels / --no-disable-labels`: Disable GitHub label operations (default: false).
- `--check-labels / --no-check-labels`: Enable checking for existing @auto-coder label (default: enabled).
- `--skip-main-update / --no-skip-main-update`: Skip merging base branch into PR when checks fail (default: skip).
- `--ignore-dependabot-prs / --no-ignore-dependabot-prs`: Skip non-ready dependency-bot PRs (default: false).
- `--force-clean-before-checkout / --no-force-clean-before-checkout`: Force clean workspace before checkout (default: false).
- `--enable-graphrag / --disable-graphrag`: Enable GraphRAG integration (default: enabled).
- `--only TEXT`: Process only a specific issue/PR by URL or number.
- `--force-reindex`: Force GraphRAG code analysis reindexing.
- `--log-level [DEBUG|INFO|WARNING|ERROR|CRITICAL]`: Set logging level (default: INFO).
- `--log-file TEXT`: Log file path.
- `--verbose`: Enable verbose logging.

#### `create-feature-issues`

Analyze repository and create feature enhancement issues.

```bash
auto-coder create-feature-issues [OPTIONS]
```

**Options:**

- `--repo TEXT`: GitHub repository.
- `--disable-labels / --no-disable-labels`: Disable GitHub label operations.
- `--enable-graphrag / --disable-graphrag`: Enable GraphRAG integration.
- `--force-reindex`: Force GraphRAG reindexing.
- `--log-level`: Set logging level.
- `--verbose`: Enable verbose logging.

#### `fix-to-pass-tests`

Run local tests and repeatedly request LLM fixes until tests pass.

```bash
auto-coder fix-to-pass-tests [OPTIONS]
```

**Options:**

- `--disable-labels / --no-disable-labels`: Disable GitHub label operations.
- `--max-attempts INTEGER`: Maximum fix attempts.
- `--enable-graphrag / --disable-graphrag`: Enable GraphRAG integration.
- `--force-reindex`: Force GraphRAG reindexing.
- `--log-level`: Set logging level.
- `--verbose`: Enable verbose logging.

### Authentication

Basically, just run `gh auth login`. When using the Gemini backend, running `gemini login` allows you to use it without setting API keys in environment variables (the --model flag is ignored for the codex backend).


#### Regarding Qwen Usage (Authentication)
- Qwen OAuth (recommended):
  - Run `qwen` once, and after authenticating your qwen.ai account in the browser, it will be automatically available.
  - Running the `/auth` command midway will switch to Qwen OAuth.
  - Reference: Qwen Code official repository (Authorization section): https://github.com/QwenLM/qwen-code
- Automatic fallback when limits are reached:
  - Auto-Coder prioritizes configured OpenAI-compatible endpoints and only returns to Qwen OAuth when all API keys are exhausted.
  - Configuration file location: `~/.auto-coder/qwen-providers.toml` (path can be overridden with `AUTO_CODER_QWEN_CONFIG`, directory can be specified with `AUTO_CODER_CONFIG_DIR`).
  - TOML example:

    ```toml
    [[qwen.providers]]
    # Option 1: Alibaba Cloud ModelStudio
    name = "modelstudio"
    api_key = "dashscope-..."  # Set the obtained API key
    # base_url and model can be omitted to use defaults (dashscope-compatible / qwen3-coder-plus)

    [[qwen.providers]]
    # Option 2: OpenRouter Free Tier
    name = "openrouter"
    api_key = "openrouter-..."
    model = "qwen/qwen3-coder:free"  # Uses default when omitted
    ```

  - Fallback occurs in the order written (API key → OAuth). If only API keys are filled in, default URL/model is applied, and `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` are automatically injected at runtime.
- OpenAI-compatible mode:
  - Available by setting the following environment variables.
    - `OPENAI_API_KEY` (required)
    - `OPENAI_BASE_URL` (specify according to provider)
    - `OPENAI_MODEL` (example: `qwen3-coder-plus`)
  - The Qwen backend of this tool uses non-interactive mode with `qwen -p/--prompt`, and the model follows the `--model/-m` flag or `OPENAI_MODEL` (--model takes precedence if both are specified).

#### Regarding Auggie Usage
- Install CLI with `npm install -g @augmentcode/auggie`.
- This tool calls `auggie --print --model <model name> "<prompt>"` in non-interactive mode.
- Auggie backend calls are limited to 20 per day. The 21st and subsequent calls automatically stop until the date changes and fallback to other backends.
- If `--model` is not specified, `GPT-5` is used as the default model. You can override with the `--model` option to specify any model.

### CLI Commands

#### Processing Issues and PRs
```bash
# Run with configuration file defaults
auto-coder process-issues --repo owner/repo

# Process only specific Issue/PR (by number)
auto-coder process-issues --repo owner/repo --only 123

# Process only specific PR (by URL)
auto-coder process-issues --repo owner/repo --only https://github.com/owner/repo/pull/456
```

#### Creating Feature Proposal Issues
```bash
# Run with configuration file defaults
auto-coder create-feature-issues --repo owner/repo
```

#### Auto-fix until tests pass (fix-to-pass-tests)
Run local tests, and if they fail, ask the LLM for minimal fixes and repeatedly re-execute. Stops with an error if the LLM doesn't make any edits.

```bash
# Run with configuration file defaults
auto-coder fix-to-pass-tests

# Specify number of attempts (example: max 5 times)
auto-coder fix-to-pass-tests --max-attempts 5
```

### Command Options

#### `process-issues`
- `--repo`: GitHub repository (owner/repo format)
- `--skip-main-update/--no-skip-main-update`: Switch behavior of whether to merge PR base branch into PR branch before attempting fixes when PR checks fail (default: skip base branch merge).
  - Default: `--skip-main-update` (skip)
  - Specify `--no-skip-main-update` to explicitly perform base branch merge
- `--ignore-dependabot-prs/--no-ignore-dependabot-prs`: Exclude Dependabot PRs from processing (default: do not exclude)
- `--only`: Process only specific Issue/PR (URL or number specification)

Options:
- `--github-token`: Manual specification when not using gh CLI authentication

#### `create-feature-issues`
- `--repo`: GitHub repository (owner/repo format)

Options:
- `--github-token`: Manual specification when not using gh CLI authentication

#### `fix-to-pass-tests`
- `--max-attempts`: Maximum number of test fix attempts (uses engine default when omitted)

Behavior Specification:
- Test execution uses `scripts/test.sh` if it exists, otherwise runs `pytest -q --maxfail=1`.
  - `scripts/test.sh` supports the following features:
    - Prefers using uv runner for consistent, reproducible environments
    - Falls back to system Python's pytest when uv is not installed
    - Can optionally activate local virtual environment by setting `AC_USE_LOCAL_VENV=1`
    - Always enables automatic dependency synchronization with uv
- For each failure, extracts important parts from error output and asks LLM for minimal fixes.
- After fixes, stages and commits. Stops with error if there are no changes at all (`nothing to commit`).

#### Lock Management Commands

Auto-Coder includes a lock mechanism to prevent concurrent executions that could cause conflicts or data corruption. When you run an auto-coder command, it automatically acquires a lock to ensure only one instance is running at a time.

**Automatic Lock Behavior:**
- Auto-Coder automatically acquires a lock before executing any command (except read-only commands like `config` and `unlock`)
- If another instance is already running, you'll see an error message with lock information
- The lock is automatically released when the command completes
- Lock files are stored in the `.git` directory of your repository

**For Developers:**
The `LockManager` class supports Python's context manager protocol for safe lock management:
```python
from auto_coder.lock_manager import LockManager

# Recommended: Using context manager (automatic cleanup)
with LockManager() as lock:
    # Lock is automatically acquired here
    do_work()
# Lock is automatically released here, even if an exception occurs

# Alternative: Manual management
lock = LockManager()
if lock.acquire():
    try:
        do_work()
    finally:
        lock.release()
```

**Manual Lock Control:**

```bash
# Remove a lock file (use when you know the process has terminated)
auto-coder lock unlock

# Force remove a lock file (use with caution - only if the process is not running)
auto-coder lock unlock --force
```

**Lock Information:**
When a lock is detected, Auto-Coder displays:
- Process ID (PID) of the running instance
- Hostname where the process is running
- Start time of the process
- Status (running or stale)

If the status shows "stale lock" (the process is no longer running), you can safely remove the lock with `auto-coder lock unlock`. The `--force` flag should only be used if you're certain the process isn't running or if you need to override a lock for emergency recovery.

**Stale Lock Detection:**
Auto-Coder detects stale locks by checking if the process associated with the lock is still active. If the process has terminated but left behind a lock file, you can remove it without the `--force` flag. The system will automatically detect that the process is no longer running.

## Branch Naming Best Practices

### Naming Conventions

#### Issue Branches

- **Format**: `issue-<number>` (e.g., `issue-699`)
- **Use case**: Use for main development work on an issue
- **Example**:
  ```bash
  git checkout -b issue-699
  ```

#### Attempt Branches

- **Format**: `issue-<number>_attempt-<number>` (e.g., `issue-699_attempt-1`)
- **Use case**: Use when retrying work after a failed PR merge or regression
- **Example**:
  ```bash
  git checkout -b issue-699_attempt-1
  ```

> **Note**: Prior to v1.x.x, attempt branches used slash separator (`issue-699/attempt-1`).
> The new underscore format prevents Git errors when both base and attempt branches exist.
> Both formats are supported for backward compatibility.

### Git Ref Namespace Limitations

Git stores branch references as filesystem paths under `.git/refs/heads/`. This creates
a limitation: **a path cannot be both a file and a directory**.

#### Conflicting Names ❌

The following branch name combinations will conflict:

- Having both `issue-699` AND `issue-699/attempt-1` (legacy format)
- Having both `feature` AND `feature/new-api`
- Having both `pr-123` AND `pr-123/fix-typo`

#### Resolution

If you encounter a branch name conflict:

1. **Delete the parent branch**:
   ```bash
   git branch -D issue-699
   ```

2. **Then create the child branch**:
   ```bash
   git checkout -b issue-699/attempt-1
   ```

3. **Alternative**: Use a different branch name that doesn't conflict

#### Prevention

- Plan your branch naming hierarchy in advance
- Clean up old branches regularly
- Use consistent naming patterns
- Prefer the underscore format for attempt branches to avoid namespace conflicts

### Common Scenarios

#### When to Use Attempt Branches

Use attempt branches when:

- Your PR failed CI/CD checks and you need to retry
- You discovered a regression after merging
- You need to try a different approach to solving the same issue
- You want to preserve the original attempt for reference

#### How to Clean Up Old Branches

```bash
# List all local branches
git branch

# Delete a local branch
git branch -d issue-699_attempt-1

# Delete a remote branch
git push origin --delete issue-699_attempt-1

# Bulk delete merged local branches
git branch --merged | grep -v "\*" | xargs -n 1 git branch -d
```

#### Recommended Workflow

1. **Initial work**: Create `issue-<number>` branch
2. **If PR fails**: Keep the original branch for reference
3. **Retry attempt**: Create `issue-<number>_attempt-1` from the base branch
4. **Subsequent attempts**: Continue with `issue-<number>_attempt-2`, etc.
5. **After successful merge**: Delete old attempt branches locally and remotely

### Automated Conflict Detection

As of v1.x.x, `auto-coder` automatically detects branch name conflicts and
provides clear error messages with resolution steps.

```bash
# If you see this error:
ERROR: Cannot create branch 'issue-699/attempt-1': conflicts with existing branch 'issue-699'

# Resolve by deleting the conflicting branch:
git branch -D issue-699
```

The system checks for conflicts before creating new branches and will:

1. **Detect parent-child conflicts**: Prevent creating `branch/name` if `branch` exists
2. **Detect child conflicts**: Prevent creating `branch` if `branch/*` branches exist
3. **Provide clear error messages**: Include the name of the conflicting branch
4. **Suggest resolution steps**: Tell you exactly which branch to delete

### Migration from Legacy Format

If you have existing branches with the old slash format (`issue-X/attempt-Y`), they will
continue to work. However, new attempt branches will use the underscore format.

For detailed migration instructions, see the [Branch Naming Migration Guide](docs/MIGRATION_GUIDE_BRANCH_NAMING.md).

To migrate an existing branch manually:
```bash
# Rename local branch (legacy slash to underscore)
git branch -m issue-699/attempt-1 issue-699_attempt-1

# Delete old remote branch
git push origin --delete issue-699/attempt-1

# Push renamed branch
git push origin issue-699_attempt-1
```

## Work-in-Progress (WIP) Branch Resumption

When you run `auto-coder process` from a non-main branch, the system automatically detects work in progress and resumes processing:

### Automatic WIP Detection

1. Detects current branch is not `main`
2. Searches for open PR with matching head branch
3. If PR found, resumes work on that PR
4. If no PR found, extracts issue number from branch name (e.g., `fix/123-description` → issue #123)

### Label Handling in WIP Mode

**Important**: When resuming WIP branches, `auto-coder` **ignores** the `\@auto-coder` label state:
- Processing continues even if `\@auto-coder` label already exists on the PR/issue
- This allows you to retry/continue work without manually removing labels
- The label will be re-added if not present, or left as-is if already present

**Regular Mode** (not on WIP branch):
- Checks for `\@auto-coder` label before processing
- Skips processing if label already exists (prevents concurrent work)
- Use `--check-labels=false` to override this behavior

### Example Usage

```bash
# On branch 'fix/toml-parsing' with PR #704
auto-coder process
# → Automatically resumes PR #704, ignoring existing \@auto-coder label

# Explicit target (bypasses label checks)
auto-coder process --only 704

# Force label checking even on WIP branch (not recommended)
auto-coder process --check-labels
```

## Parent Issue Auto-Reopen

When processing child issues in a parent-child relationship, Auto-Coder automatically handles closed parent issues:

### Auto-Reopen Behavior

**When processing a child issue whose parent is closed:**
1. The system automatically reopens the parent issue before continuing
2. Branch selection and base branch selection use the parent issue context
3. Attempt tracking operates within the parent issue context
4. This ensures proper workflow continuity and prevents branch naming conflicts

### Why This Matters

This behavior is critical for maintaining consistency when:
- Working with hierarchical issue structures
- Managing parent-child issue dependencies
- Ensuring branch and attempt tracking remain coherent
- Preventing branch naming and base selection issues during child issue processing

### Example

If you have issue #100 (parent) with child issue #101:
- Issue #101 is being processed
- Issue #100 (parent) is closed
- Auto-Coder automatically reopens #100
- All branch operations and attempts now use #100's context
- Processing continues with proper parent context maintained

## Configuration

### Configuration File Locations

Auto-Coder supports configuration files in multiple locations with the following priority:

1. **Local configuration** (highest priority): `.auto-coder/llm_config.toml` in the current directory
2. **Home configuration**: `~/.auto-coder/llm_config.toml` in your home directory
3. **Default configuration**: Auto-generated if no configuration file exists

#### When to Use Local Configuration

Use local configuration (`.auto-coder/llm_config.toml`) when:
- You need project-specific LLM backend settings
- You want to version-control your configuration with your project
- You're working in a team and want to share configuration
- You're testing different configurations without affecting global settings
- You're working in container/CI environments where configuration should be part of the project

#### When to Use Home Configuration

Use home configuration (`~/.auto-coder/llm_config.toml`) when:
- You want the same configuration across all projects
- You're working on personal projects with consistent settings
- You want to keep API keys and credentials out of project repositories

#### Configuration Search Behavior

When Auto-Coder starts, it searches for configuration files in this order:
1. Checks if `.auto-coder/llm_config.toml` exists in the current working directory
2. If not found, falls back to `~/.auto-coder/llm_config.toml` in the home directory
3. If neither exists, creates a default configuration in the home directory

This approach is **backward compatible**: existing users with only home directory configs will see no change in behavior.

### Configuration File (TOML)

Auto-Coder uses a TOML configuration file for backend settings. You can place this file in either location mentioned above.

**Note:** By default, all backends have `enabled = true`. You only need to specify `enabled = false` to disable a backend.

#### Custom Backend Names

You can define custom backend names using the `backend_type` field. This is useful for:
- Using OpenRouter with specific model names
- Configuring multiple versions of the same backend
- Using OpenAI-compatible providers

Example:
```toml
[backends.grok-4.1-fast]
backend_type = "codex"  # Use codex CLI for OpenAI-compatible APIs
model = "grok-4.1-fast"
openai_api_key = "your-openrouter-api-key"
openai_base_url = "https://openrouter.ai/api/v1"
```

Supported `backend_type` values: `codex`, `codex-mcp`, `gemini`, `qwen`, `auggie`, `claude`

For detailed configuration examples and troubleshooting, including the new fallback backend feature, see [Configuration Guide](docs/configuration.md).

#### Configuring Model Provider

For backends using `backend_type = "codex"`, you can specify the model provider using the `model_provider` field. This tells the codex CLI which provider to use when making API calls.

##### Example: Using OpenRouter

```toml
[grok-4.1-fast]
enabled = true
model = "x-ai/grok-4.1-fast:free"
backend_type = "codex"
model_provider = "openrouter"
```

This configuration is equivalent to running:
```bash
codex -c model="x-ai/grok-4.1-fast:free" -c model_provider=openrouter
```

##### Supported Providers

The `model_provider` field supports any provider that the codex CLI recognizes, such as:
- `openrouter` - For OpenRouter API
- `anthropic` - For Anthropic Claude
- `openai` - For OpenAI models

> **Note:** You still need to configure provider-specific settings (like API endpoints and authentication) in your codex configuration file (`~/.codex/config.toml`) or via environment variables. The `model_provider` field simply tells codex which provider configuration to use.

##### Complete Example with OpenRouter

Here's a complete example showing how to configure a custom backend with OpenRouter:

```toml
[backends.openrouter-grok]
enabled = true
backend_type = "codex"
model = "x-ai/grok-4.1-fast:free"
model_provider = "openrouter"
openai_api_key = "sk-or-v1-..."  # Your OpenRouter API key
openai_base_url = "https://openrouter.ai/api/v1"
```

Make sure your codex configuration file (`~/.codex/config.toml`) contains the appropriate provider settings. For example:

```toml
# ~/.codex/config.toml
[openrouter]
api_base = "https://openrouter.ai/api/v1"
default_model = "x-ai/grok-4.1-fast:free"
```

Check the codex CLI documentation for the full list of supported providers and their configuration options.

#### Configuration Management

To manage the configuration file, use the built-in config commands:
```bash
# Show current configuration
auto-coder config show

# Edit configuration file
auto-coder config edit

# Validate configuration
auto-coder config validate

# Create backup of configuration
auto-coder config backup

# Interactive setup wizard
auto-coder config setup

# Show usage examples
auto-coder config examples

# Migrate from environment variables
auto-coder config migrate
```

Example configuration file (`.auto-coder/llm_config.toml` or `~/.auto-coder/llm_config.toml`):
```toml
version = "1.0.0"
created_at = "2023-01-01T00:00:00"
updated_at = "2023-01-01T00:00:00"

[backend]
order = ["gemini", "qwen", "claude"]
default = "gemini"

[backends.codex]
api_key = ""
base_url = ""
model = "codex"
temperature = 0.7
timeout = 30
max_retries = 3
usage_limit_retry_count = 0
usage_limit_retry_wait_seconds = 0

[backends.codex_mcp]
api_key = ""
base_url = ""
model = "codex-mcp"
temperature = 0.7
timeout = 30
max_retries = 3
usage_limit_retry_count = 0
usage_limit_retry_wait_seconds = 0

[backends.gemini]
api_key = "your-gemini-api-key"  # Alternatively use GEMINI_API_KEY env var
base_url = ""
model = "gemini-2.5-pro"
temperature = 0.7
timeout = 30
max_retries = 3
usage_limit_retry_count = 3
usage_limit_retry_wait_seconds = 30
always_switch_after_execution = false

[backends.qwen]
api_key = "your-qwen-api-key"  # Alternatively use OPENAI_API_KEY env var
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"  # Or your OpenAI-compatible endpoint
model = "qwen3-coder-plus"
temperature = 0.7
timeout = 30
max_retries = 3
usage_limit_retry_count = 2
usage_limit_retry_wait_seconds = 60
always_switch_after_execution = false

[backends.claude]
api_key = "your-claude-api-key"  # Alternatively use ANTHROPIC_API_KEY env var
base_url = ""
model = "sonnet"
temperature = 0.7
timeout = 30
max_retries = 3
usage_limit_retry_count = 5
usage_limit_retry_wait_seconds = 45

[backends.auggie]
api_key = ""
base_url = ""
model = "GPT-5"
temperature = 0.7
timeout = 30
max_retries = 3
usage_limit_retry_count = 1
usage_limit_retry_wait_seconds = 120

[message_backend]
order = ["claude"]
default = "claude"
```

### Supported Backend Types

Auto-Coder supports multiple backend types. When using custom backend names, you must specify the `backend_type` field:

| Backend Type | Description | Required Tools | Use Cases |
|-------------|-------------|----------------|-----------|
| `codex` | OpenAI Codex and OpenAI-compatible APIs | Codex CLI | OpenRouter, Azure OpenAI, custom endpoints |
| `codex-mcp` | Codex with MCP support | Codex CLI | Advanced MCP integrations |
| `gemini` | Google Gemini | Gemini CLI | Direct Google API integration |
| `qwen` | Qwen Code | Qwen CLI | Native Qwen CLI with OAuth |
| `claude` | Anthropic Claude | Claude CLI | Direct Anthropic API integration |
| `jules` | Jules AI Assistant | Jules CLI | Session-based AI with PR feedback loop |
| `auggie` | Auggie | Auggie CLI (`npm install -g @augmentcode/auggie`) | Auggie integration |

**Important:** Custom backend names require the `backend_type` field. Standard backend names (`codex`, `gemini`, `qwen`, `claude`, `jules`, `auggie`) work without this field.

For more details, see:
- [Configuration Guide](docs/configuration.md)
- [OpenRouter Setup Guide](OPENROUTER_SETUP.md)
- [Example Configuration File](docs/llm_backend_config.example.toml)

### Jules Mode Configuration

Jules mode is a special operational mode that adds the `jules` label to issues and processes them using the Jules AI assistant. Jules mode can be enabled in two ways:

#### 1. CLI Flag (Per-Command)

You can enable or disable Jules mode for each command using the `--jules-mode` or `--no-jules-mode` flag:

```bash
# Enable Jules mode (default: ON)
auto-coder process-issues --jules-mode

# Disable Jules mode
auto-coder process-issues --no-jules-mode
```

Jules mode is **ON by default** for `process-issues` commands.

#### 2. Configuration File (Persistent)

You can configure Jules as a backend in your `llm_config.toml` file for persistent settings:

```toml
[backend]
# Add jules to your backend order
order = ["jules", "gemini", "codex", "claude"]
default = "codex"

[backends.jules]
enabled = true
backend_type = "jules"
# No additional configuration needed - uses Jules CLI with OAuth
```

**Configuration Options:**

- **`enabled`**: Set to `true` to enable Jules backend, `false` to disable (default: true)
- **`backend_type`**: Must be set to `"jules"` for Jules backend configuration
- **`model`**: Optional - Jules model name (defaults to Jules CLI default)
- **`temperature`**: Optional - Controls randomness (default: 0.7)
- **`timeout`**: Optional - Request timeout in seconds (default: 300)
- **`max_retries`**: Optional - Maximum retry attempts (default: 3)

**Notes:**
- Jules uses OAuth authentication - no API keys required
- Requires Jules CLI to be installed and authenticated
- Sessions are tracked in `~/.auto-coder/<repo>/cloud.csv` files
- Jules automatically comments on issues with session information

For more details about Jules mode architecture and migration from legacy Jules label logic, see the [Migration Guide: v2026.2.0](docs/MIGRATION_GUIDE_v2026.2.0.md).

### Retry Configuration

Auto-Coder supports automatic retry on usage limit errors for LLM backends. This feature helps handle temporary rate limits or quota exhaustion by allowing backends to retry requests before rotating to the next available backend.

#### Configuration Fields

Each backend in the TOML configuration file can specify retry behavior using these fields:

- **`usage_limit_retry_count`**: Number of times to retry when a usage limit error is encountered (default: 0)
  - Set to 0 to disable retries (immediately rotate to next backend)
  - Set to a positive number to retry on the same backend before rotating

- **`usage_limit_retry_wait_seconds`**: Number of seconds to wait between retry attempts (default: 0)
  - This delay allows the backend's usage quota to reset or rate limits to expire
  - Recommended values depend on the backend provider's rate limiting policies

#### Behavior

**When retry is configured (usage_limit_retry_count > 0):**
1. Backend encounters a usage limit error
2. System retries the same backend up to `usage_limit_retry_count` times
3. Waits `usage_limit_retry_wait_seconds` between each retry attempt
4. If all retries are exhausted, rotates to the next backend in the configured order
5. Continues through the backend rotation until a successful response is received or all backends are exhausted

**When retry is not configured (usage_limit_retry_count = 0, default):**
1. Backend encounters a usage limit error
2. Immediately rotates to the next backend in the configured order
3. No retry attempts on the same backend

#### Example Configuration

The example configuration above demonstrates different retry settings for different backends:

- **Gemini**: Retries 3 times with 30-second delays between attempts
- **Qwen**: Retries 2 times with 60-second delays between attempts
- **Claude**: Retries 5 times with 45-second delays between attempts (more aggressive retry policy)
- **Auggie**: Retries 1 time with 120-second delays (longer wait time due to daily limits)
- **Codex/Codex-MCP**: No retries (0 count, 0 wait) - immediately rotates on usage limits

#### Recommended Settings

Different LLM providers have different rate limiting behaviors:

- **Gemini**: 3-5 retries with 30-60 second waits typically works well
- **Claude**: 3-5 retries with 30-60 second waits
- **Qwen**: 2-3 retries with 60-120 second waits, especially for API keys
- **Auggie**: 1-2 retries with 120+ seconds (due to strict daily call limits)
- **Codex**: 0-1 retries (OpenAI Codex has strict rate limits)

Adjust these values based on your usage patterns and the specific rate limits of your LLM providers.

### Post-Execution Backend Rotation

Enable `always_switch_after_execution` on individual backends to rotate to the next backend in `backend.order` after every successful run. This is useful when you want round-robin usage across providers (for example, to spread traffic, avoid hitting soft limits, or comply with single-execution quotas) instead of sticking to the same backend until a failure occurs.

Example:

```toml
[backend]
order = ["gemini", "qwen", "claude"]
default = "gemini"

[backends.gemini]
model = "gemini-2.5-pro"
always_switch_after_execution = true  # switch to qwen after each gemini call

[backends.qwen]
model = "qwen3-coder-plus"
always_switch_after_execution = true  # continue rotation to claude next
```

If `always_switch_after_execution` is omitted or set to `false`, Auto-Coder keeps using the active backend until a retry/rotation condition is triggered.

### Session Resume Configuration

Auto-Coder supports session resumption for backends that maintain stateful sessions (like Claude). When the same backend is used consecutively, the system can automatically resume the previous session instead of starting a new one, providing better context continuity.

#### How Session Resume Works

When configured, the system:
1. Tracks the session ID from the last backend execution
2. Detects when the same backend is used consecutively
3. Automatically injects resume options with the previous session ID
4. The backend resumes the previous session with full context

#### Configuration

Add the `options_for_resume` field to your backend configuration in `llm_config.toml`:

```toml
[backends.claude]
model = "sonnet"
# Resume options with session ID placeholder
options_for_resume = ["--resume", "[sessionId]"]
```

The `[sessionId]` placeholder will be automatically replaced with the actual session ID from the previous execution.

#### Example: Claude Backend with Session Resume

```toml
[backend]
order = ["claude", "gemini", "codex"]
default = "claude"

[backends.claude]
enabled = true
model = "sonnet"
timeout = 60
max_retries = 3
# Enable session resume for Claude
options_for_resume = ["--resume", "[sessionId]"]
```

#### How It Works

When you run multiple operations consecutively:

```bash
# First operation - starts new session
auto-coder process-issues --only 123
# Session ID: abc123 (tracked internally)

# Second operation - resumes previous session
auto-coder process-issues --only 124
# Uses: claude --resume abc123
```

The second operation automatically resumes session `abc123`, providing Claude with context from the first operation.

#### Supported Backends

Currently, session resume is implemented for:
- **Claude**: Uses `--resume <session-id>` flag

Other backends may support session resume if they provide similar session management capabilities.

#### Notes

- Session IDs are tracked per backend
- Session state is persisted to `~/.auto-coder/backend_session_state.json` so consecutive executions can resume when the backend supports it
- Resume only happens when the same backend is used consecutively
- If a different backend is used, the session tracking resets
- The `options_for_resume` list can include multiple options if needed

### Timeout Handling and Automatic Backend Fallback

Auto-Coder includes automatic timeout handling that triggers fallback to the next configured backend when an LLM command exceeds its configured timeout. This ensures operations continue smoothly even when a backend becomes unresponsive or slow.

#### How Timeout Fallback Works

**When a timeout occurs:**
1. The current backend command is terminated when it exceeds its configured timeout duration
2. Auto-Coder catches the `AutoCoderTimeoutError` exception
3. The system automatically switches to the next backend in the configured `backend.order` list
4. The operation is retried with the new backend
5. This continues through all configured backends until a successful response is received or all backends are exhausted

**Timeout behavior varies by operation:**

- **Normal Operations (`process-issues`, `create-feature-issues`)**:
  - Timeouts trigger automatic fallback to the next backend
  - System continues through all backends in order
  - If all backends timeout, the last timeout error is raised

- **Test Fix Operations (`fix-to-pass-tests`)**:
  - Timeouts trigger immediate fallback to the next backend
  - Operation is retried once with the new backend
  - If the retry also times out, the error is propagated

#### Example Scenario

With the following configuration:
```toml
[backend]
order = ["gemini", "qwen", "claude"]
default = "gemini"

[backends.gemini]
model = "gemini-2.5-pro"
timeout = 30

[backends.qwen]
model = "qwen3-coder-plus"
timeout = 30

[backends.claude]
model = "sonnet"
timeout = 30
```

**Execution flow:**
1. First attempt uses `gemini` backend
2. If `gemini` times out after 30 seconds → automatically switch to `qwen`
3. If `qwen` times out after 30 seconds → automatically switch to `claude`
4. If `claude` succeeds, return the result
5. If `claude` also times out, raise the timeout error

#### Configuration

Timeout values are configured per backend in the TOML configuration file:

```toml
[backends.gemini]
model = "gemini-2.5-pro"
timeout = 30  # seconds
```

**Timeout recommendations by provider:**
- **Gemini**: 30-60 seconds ( Google's API is generally fast)
- **Claude**: 30-60 seconds (Anthropic's API is responsive)
- **Qwen**: 60-120 seconds (Alibaba's API may need more time)
- **Codex**: 30-60 seconds (OpenAI's Codex is typically fast)
- **Auggie**: 60-120 seconds ( Auggie has daily call limits)

Adjust timeout values based on your network conditions and the complexity of tasks you're running.

#### Logging

Timeout fallback events are logged for debugging:
```
WARNING - Timeout error on backend 'gemini', switching to next backend
```

Check the logs at `~/.auto-coder/logs/llm_output.jsonl` for detailed information about timeout occurrences and backend rotations.

### Environment Variables

Environment variables can be used to override configuration file values or provide sensitive information like API keys:

| Variable Name | Description | Default Value | Required |
|--------------|-------------|---------------|----------|
| `GITHUB_TOKEN` | GitHub API token (to override gh CLI authentication) | - | ❌ |
| `GITHUB_API_URL` | GitHub API URL | `https://api.github.com` | ❌ |
| `MAX_ISSUES_PER_RUN` | Maximum issues to process per run | `-1` | ❌ |
| `MAX_PRS_PER_RUN` | Maximum PRs to process per run | `-1` | ❌ |
| `LOG_LEVEL` | Log level | `INFO` | ❌ |
| `AUTO_CODER_DEFAULT_BACKEND` | Set default backend (e.g., 'gemini', 'codex') | `codex` | ❌ |
| `AUTO_CODER_<BACKEND>_API_KEY` | Set API key for specific backend | - | ❌ |
| `AUTO_CODER_OPENAI_API_KEY` | Set OpenAI-compatible API key | - | ❌ |
| `AUTO_CODER_OPENAI_BASE_URL` | Set OpenAI-compatible base URL | - | ❌ |

**Backend-specific environment variables:**
- `AUTO_CODER_CODEX_API_KEY`, `AUTO_CODER_GEMINI_API_KEY`, `AUTO_CODER_QWEN_API_KEY`, `AUTO_CODER_CLAUDE_API_KEY`, `AUTO_CODER_AUGGIE_API_KEY`
- Model-specific variables: `AUTO_CODER_<BACKEND>_MODEL` (e.g., `AUTO_CODER_GEMINI_MODEL`)

`MAX_ISSUES_PER_RUN` and `MAX_PRS_PER_RUN` are set to unlimited (`-1`) by default. Specify positive integers if you want to limit the number of items processed.

**Migration from old environment variables:**
If you were using the old environment variables (`GEMINI_API_KEY`, `OPENAI_API_KEY`, etc.), you can migrate them automatically:
```bash
auto-coder config migrate
```

## Logging and Monitoring

### Overview

Auto-Coder provides comprehensive logging for all LLM interactions using the `LLMOutputLogger` class. Logs are written in structured JSON Lines format (one JSON object per line) for easy parsing and analysis.

### Default Log Location

```
~/.auto-coder/logs/llm_output.jsonl
```

### Environment Variable Control

Enable or disable logging using the `AUTO_CODER_LLM_OUTPUT_LOG_ENABLED` environment variable:

```bash
# Enable logging (default)
export AUTO_CODER_LLM_OUTPUT_LOG_ENABLED=1
# or: true, yes, on

# Disable logging
export AUTO_CODER_LLM_OUTPUT_LOG_ENABLED=0
# or: false, no, off
```

### Example Log Entry

Each log entry contains structured data about an LLM interaction:

```json
{
  "timestamp": "2025-11-24T10:30:45.123Z",
  "event_type": "llm_interaction",
  "backend": "codex",
  "model": "codex",
  "prompt_length": 15420,
  "response_length": 8956,
  "duration_ms": 1234,
  "status": "success"
}
```

### Parsing Logs with jq

```bash
# Extract all successful interactions
grep "^{" ~/.auto-coder/logs/llm_output.jsonl | jq 'select(.status == "success")'

# Find errors
grep "^{" ~/.auto-coder/logs/llm_output.jsonl | jq 'select(.status == "error")'

# Filter by backend
grep "^{" ~/.auto-coder/logs/llm_output.jsonl | jq 'select(.backend == "codex")'

# Get statistics
grep "^{" ~/.auto-coder/logs/llm_output.jsonl | jq -r '.backend' | sort | uniq -c
```

### Parsing Logs with Python

```python
import json
from pathlib import Path

log_file = Path.home() / ".auto-coder" / "logs" / "llm_output.jsonl"

# Read all log entries
with log_file.open("r") as f:
    for line in f:
        data = json.loads(line.strip())
        print(f"{data['timestamp']}: {data['backend']} - {data['status']}")

# Filter by backend
with log_file.open("r") as f:
    codex_logs = []
    for line in f:
        data = json.loads(line.strip())
        if data['backend'] == 'codex':
            codex_logs.append(data)

print(f"Found {len(codex_logs)} Codex interactions")
```

### Log Entry Fields

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | string | ISO 8601 timestamp with timezone |
| `event_type` | string | Event type: `llm_request`, `llm_response`, or `llm_interaction` |
| `backend` | string | Backend name (e.g., "codex", "gemini", "qwen") |
| `model` | string | Model name |
| `prompt_length` | integer | Prompt length in characters |
| `response_length` | integer | Response length in characters |
| `duration_ms` | float | Request duration in milliseconds |
| `status` | string | Request status (e.g., "success", "error") |
| `error` | string (optional) | Error message if status is "error" |
| Additional metadata | any | Custom fields from the interaction |

### Console Output

In addition to JSON log files, Auto-Coder prints user-friendly execution summaries to console:

```
============================================================
🤖 Codex CLI Execution Summary
============================================================
Backend: codex
Model: codex
Prompt Length: 15420 characters
Response Length: 8956 characters
Duration: 1234ms
Status: SUCCESS
============================================================
```

This provides immediate feedback during execution while detailed logs are saved to the file.

### Custom Log Path

You can configure a custom log path programmatically:

```python
from src.auto_coder.llm_output_logger import LLMOutputLogger

# Use custom path
logger = LLMOutputLogger(log_path="/custom/path/log.jsonl")

# Disable logging
logger = LLMOutputLogger(enabled=False)
```

For more detailed documentation, see [docs/llm_output_logger_usage.md](docs/llm_output_logger_usage.md).

## Label-Based Prompt Routing

Auto-Coder includes an intelligent label-based prompt system that dynamically selects appropriate prompt templates based on GitHub issue and PR labels. This enables more targeted and effective AI-driven automation by using context-specific instructions.

### Overview

The label-based prompt system works by:

1. **Analyzing Labels**: Examining labels on GitHub issues and PRs
2. **Priority Resolution**: Selecting the highest priority applicable label when multiple labels exist
3. **Prompt Selection**: Using the selected label to choose an appropriate prompt template
4. **Special Handling**: Applying special behaviors for breaking-change and urgent issues

### Default Label Categories

**Breaking-Change Labels** (highest priority):
- `breaking-change`, `breaking`, `api-change`, `deprecation`, `version-major`

**Priority Labels**:
- `urgent`, `high-priority`, `critical`, `blocker`, `asap`

**Issue Type Labels**:
- `bug`, `bugfix`, `defect`, `error`, `fix`, `hotfix`, `patch`
- `enhancement`, `feature`, `improvement`, `new-feature`
- `refactor`, `optimization`, `optimisation`
- `documentation`, `docs`, `doc`, `readme`, `guide`

### Configuration

You can configure the label-based prompt system using environment variables:

#### Basic Setup

```bash
# Configure label-to-prompt mappings
export AUTO_CODER_LABEL_PROMPT_MAPPINGS='{
  "bug": "issue.bug",
  "enhancement": "issue.enhancement",
  "urgent": "issue.urgent",
  "breaking-change": "issue.breaking_change",
  "documentation": "issue.documentation"
}'

# Set priority order (highest priority first)
export AUTO_CODER_LABEL_PRIORITIES='[
  "breaking-change",
  "urgent",
  "bug",
  "enhancement",
  "documentation"
]'
```

#### PR Label Copying

The system can automatically copy semantic labels from issues to their corresponding PRs:

```bash
# Enable/disable (default: true)
export AUTO_CODER_PR_LABEL_COPYING_ENABLED='true'

# Maximum labels to copy (default: 3, range: 0-10)
export AUTO_CODER_PR_MAX_LABELS='3'

# Configure PR label mappings
export AUTO_CODER_PR_LABEL_MAPPINGS='{
  "breaking-change": ["breaking-change", "breaking"],
  "urgent": ["urgent", "critical"],
  "bug": ["bug", "bugfix", "defect"],
  "enhancement": ["enhancement", "feature"],
  "documentation": ["documentation", "docs"]
}'

# Set PR label priorities
export AUTO_CODER_PR_LABEL_PRIORITIES='[
  "urgent",
  "breaking-change",
  "bug",
  "enhancement",
  "documentation"
]'
```

### How It Works

**Priority Resolution Example:**

If an issue has labels: `["bug", "enhancement", "urgent"]`
And priorities are: `["urgent", "bug", "enhancement"]`
Then the system uses the `urgent` prompt (highest priority).

**Breaking-Change Detection:**

Labels in the breaking-change category trigger special handling:
- Automatically deletes failing tests that test removed features
- Provides version bump recommendations (major version bump)
- Generates migration guides
- Updates CHANGELOG with breaking changes
- Ensures backward compatibility guidance

### Complete Example Configuration

Create a `.env` file:

```bash
# Label prompt mappings
AUTO_CODER_LABEL_PROMPT_MAPPINGS='{
  "bug": "issue.bug",
  "bugfix": "issue.bug",
  "enhancement": "issue.enhancement",
  "feature": "issue.enhancement",
  "urgent": "issue.urgent",
  "critical": "issue.urgent",
  "breaking-change": "issue.breaking_change",
  "documentation": "issue.documentation"
}'

# Label priorities
AUTO_CODER_LABEL_PRIORITIES='[
  "breaking-change",
  "urgent",
  "bug",
  "enhancement",
  "documentation"
]'

# PR label mappings
AUTO_CODER_PR_LABEL_MAPPINGS='{
  "breaking-change": ["breaking-change"],
  "urgent": ["urgent", "critical"],
  "bug": ["bug", "bugfix"],
  "enhancement": ["enhancement", "feature"],
  "documentation": ["documentation"]
}'

# PR label priorities
AUTO_CODER_PR_LABEL_PRIORITIES='[
  "urgent",
  "breaking-change",
  "bug",
  "enhancement",
  "documentation"
]'

# PR label copying
AUTO_CODER_PR_LABEL_COPYING_ENABLED='true'
AUTO_CODER_PR_MAX_LABELS='3'
```

### Project-Specific Examples

See the `examples/` directory for detailed configuration templates for different project types:
- [TypeScript/JavaScript projects](examples/typescript-project-config.json)
- [Python projects](examples/python-project-config.json)
- [Comprehensive configuration](examples/label-config.json)
- [Minimal setup](examples/minimal-config.env)

### Testing Your Configuration

```bash
# Process a specific issue to test label-based prompts
auto-coder process-issues --repo owner/repo --only 123

# Check configuration
auto-coder config show
```

### Documentation

For complete documentation with all options and best practices, see:
- [Label-Based Prompt Configurations](examples/label-based-prompt-configurations.md)
- [client-features.yaml](docs/client-features.yaml) (technical specification)

## GraphRAG Integration (Experimental Feature)

Auto-Coder supports GraphRAG (Graph Retrieval-Augmented Generation) integration using Neo4j and Qdrant.

### GraphRAG Setup

1. Install dependencies for GraphRAG:
```bash
pip install -e ".[graphrag]"
```

2. Start Neo4j and Qdrant with Docker:
```bash
# Start with Docker Compose
docker compose -f docker-compose.graphrag.yml up -d

# Check status
docker compose -f docker-compose.graphrag.yml ps

# Check logs
docker compose -f docker-compose.graphrag.yml logs
```

3. GraphRAG MCP Server Setup (optional):
```bash
# Automatic setup (recommended)
# Use bundled custom MCP server (code-analysis specialized fork)
auto-coder graphrag setup-mcp

# Manual setup
cd ~/graphrag_mcp
uv sync
uv run main.py
```

**Note**: This MCP server is a custom fork of `rileylemm/graphrag_mcp`, specialized for TypeScript/JavaScript code analysis. See `docs/client-features.yaml` `external_dependencies.graphrag_mcp` section for details.

### Verifying GraphRAG Service Operation

We have prepared a script to verify that Neo4j and Qdrant are operating correctly:

```bash
# Test all (default)
python scripts/check_graphrag_services.py

# Test direct access only
python scripts/check_graphrag_services.py --direct-only

# Test MCP only
python scripts/check_graphrag_services.py --mcp-only
```

This script verifies:
- Direct access to Neo4j (Bolt protocol)
  - Database version check
  - Node creation and search
  - Relationship creation
  - Path search
- Direct access to Qdrant (HTTP API)
  - Collection creation
  - Vector insertion
  - Similarity search
  - Filtered search
- Access via GraphRAG MCP
  - Docker container status check
  - MCP server status check
  - Index status check

### Debugging in VS Code

The following debugging configurations are included in `.vscode/launch.json`:

- **Check GraphRAG Services (All)**: Test all (default)
- **Check GraphRAG Services (Direct only)**: Test direct access only
- **Check GraphRAG Services (MCP only)**: Test MCP only

### GraphRAG Connection Information

Default connection information:

| Service | URL | Credentials |
|---------|-----|-------------|
| Neo4j (Bolt) | `bolt://localhost:7687` | `neo4j` / `password` |
| Neo4j (HTTP) | `http://localhost:7474` | `neo4j` / `password` |
| Qdrant (HTTP) | `http://localhost:6333` | No authentication |
| Qdrant (gRPC) | `http://localhost:6334` | No authentication |

### Troubleshooting

#### Cannot connect to Neo4j

```bash
# Check if container is running
docker ps | grep neo4j

# Check logs
docker logs auto-coder-neo4j

# Check if port is open
nc -zv localhost 7687
```

#### Cannot connect to Qdrant

```bash
# Check if container is running
docker ps | grep qdrant

# Check logs
docker logs auto-coder-qdrant

# Check if port is open
nc -zv localhost 6333
```

## Development

### Development Environment Setup

1. Install development dependencies:
```bash
pip install -e ".[dev]"
```

2. Setup pre-commit hooks:
```bash
pre-commit install
```

### VS Code Debug Settings

The project includes the following debug configurations:

- **Auto-Coder: Create Feature Issues**: Feature proposal issue creation in outliner directory
- **Auto-Coder: Auth Status**: Authentication status check in outliner directory
- **Auto-Coder: Process Issues (Live)**: Actual processing execution in outliner directory

To start debugging:
1. Press `F5` in VS Code or open the "Run and Debug" panel
2. Select from the above configurations and run
3. Set breakpoints for step-by-step execution

### Running Tests

```bash
# Run all tests
pytest

# Run tests with coverage
pytest --cov=src/auto_coder --cov-report=html

# Run specific test file
pytest tests/test_github_client.py
```

### Code Quality Checks

```bash
# Formatting
black src/ tests/

# Import sorting
isort src/ tests/

# Linting
flake8 src/ tests/

# Type checking
mypy src/

# Type checking via pre-commit (using uv)
# Hooks are set up via pre-commit install
```

## Architecture

### Component Structure

```
src/auto_coder/
├── cli.py              # CLI entry point
├── github_client.py    # GitHub API client (singleton)
├── gemini_client.py    # Gemini AI client
├── backend_manager.py  # LLM backend manager (singleton)
├── automation_engine.py # Main automation engine
└── config.py          # Configuration management
```

### Singleton Pattern

Auto-Coder implements the singleton pattern for key components to ensure consistent state and resource management:

#### GitHubClient Singleton
The `GitHubClient` class uses a thread-safe singleton pattern:
```python
from auto_coder.github_client import GitHubClient

# Get the singleton instance
client = GitHubClient.get_instance(token, disable_labels=False)

# Subsequent calls return the same instance
client2 = GitHubClient.get_instance(token, disable_labels=False)
# client is client2  # True
```

#### LLMBackendManager Singleton
The `LLMBackendManager` class manages LLM backends as a singleton:
```python
from auto_coder.backend_manager import get_llm_backend_manager

# Initialize once with configuration
manager = get_llm_backend_manager(
    default_backend="codex",
    default_client=client,
    factories={"codex": lambda: client}
)

# Use globally
response = run_llm_prompt("Your prompt here")
```

For detailed usage patterns, see [GLOBAL_BACKEND_MANAGER_USAGE.md](GLOBAL_BACKEND_MANAGER_USAGE.md).

### Data Flow

1. **CLI** → **AutomationEngine** → **GitHubClient** (data retrieval)
2. **AutomationEngine** → **GeminiClient** (AI analysis)
3. **AutomationEngine** → **GitHubClient** (action execution)
4. **AutomationEngine** → **Report Generation**

## Output and Reports

Execution results are saved in JSON format in the `~/.auto-coder/{repository}/` directory:

- `automation_report_*.json`: Results of automation processing (saved per repository)
- `jules_automation_report_*.json`: Results of automation processing in Jules mode

Example: For `owner/repo` repository, reports are saved in `~/.auto-coder/owner_repo/`.

## Troubleshooting

### Common Issues

1. **GitHub API limits**: Wait some time before retrying if you hit rate limits
2. **Gemini API errors**: Check if API keys are correctly configured
3. **Permission errors**: Check if GitHub token has appropriate permissions

#### "Unsupported backend specified" Error

If you see this error with a custom backend name:
1. Ensure you have set `backend_type` in your configuration
2. Use a valid backend type: `codex`, `codex-mcp`, `gemini`, `qwen`, `claude`, or `auggie`
3. Ensure the required CLI tool for the `backend_type` is installed

**Example fix:**
```toml
[backends.my-custom-backend]
backend_type = "codex"  # Add this line
model = "grok-4.1-fast"
openai_api_key = "your-key"
openai_base_url = "https://openrouter.ai/api/v1"
```

See [Configuration Guide](docs/configuration.md) for detailed troubleshooting.

### Checking Logs

```bash
# Set log level to DEBUG
export LOG_LEVEL=DEBUG
auto-coder process-issues --repo owner/repo
```

## License

This project is published under the MIT license. See the [LICENSE](LICENSE) file for details.

## Contributing

We welcome pull requests and issue reports. Before contributing, please ensure:

1. Tests pass
2. Code style is consistent
3. New features include appropriate tests

## Support

If you have issues or questions, please create a GitHub issue.

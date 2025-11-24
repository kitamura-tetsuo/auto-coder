# Auto-Coder

A Python application that automates application development using an AI CLI backend. It retrieves issues and error-related PRs from GitHub to build and fix the application, and automatically creates feature-addition issues when necessary.

## Features

### 🔧 Core Features
- **GitHub API Integration**: Automatic retrieval and management of issues and PRs
- **AI Analysis (multiple backends configurable via configuration file)**: Automatic analysis of issue and PR content
- **Automated Processing**: Automatic actions based on analysis results
- **Feature Proposals**: Automatic proposal of new features from repository analysis
- **Report Generation**: Detailed reports of processing results

### 🚀 Automated Workflow
1. **Issue Processing**: Retrieve open issues and analyze with Gemini AI
2. **PR Processing**: Retrieve open PRs and evaluate risk levels
3. **Feature Proposals**: Propose new features from repository context
4. **Automatic Actions**: Add comments or auto-close based on analysis results

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

## Configuration

### Configuration File (TOML)

Auto-Coder uses a TOML configuration file for backend settings. The configuration file is located at `~/.auto-coder/llm_config.toml` by default.

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

Example configuration file (`~/.auto-coder/llm_config.toml`):
```toml
version = "1.0.0"
created_at = "2023-01-01T00:00:00"
updated_at = "2023-01-01T00:00:00"

[backend]
order = ["gemini", "qwen", "claude"]
default = "gemini"

[backends.codex]
enabled = true
api_key = ""
base_url = ""
model = "codex"
temperature = 0.7
timeout = 30
max_retries = 3
usage_limit_retry_count = 0
usage_limit_retry_wait_seconds = 0

[backends.codex_mcp]
enabled = true
api_key = ""
base_url = ""
model = "codex-mcp"
temperature = 0.7
timeout = 30
max_retries = 3
usage_limit_retry_count = 0
usage_limit_retry_wait_seconds = 0

[backends.gemini]
enabled = true
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
enabled = true
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
enabled = true
api_key = "your-claude-api-key"  # Alternatively use ANTHROPIC_API_KEY env var
base_url = ""
model = "sonnet"
temperature = 0.7
timeout = 30
max_retries = 3
usage_limit_retry_count = 5
usage_limit_retry_wait_seconds = 45

[backends.auggie]
enabled = true
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
enabled = true
model = "gemini-2.5-pro"
always_switch_after_execution = true  # switch to qwen after each gemini call

[backends.qwen]
enabled = true
model = "qwen3-coder-plus"
always_switch_after_execution = true  # continue rotation to claude next
```

If `always_switch_after_execution` is omitted or set to `false`, Auto-Coder keeps using the active backend until a retry/rotation condition is triggered.

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

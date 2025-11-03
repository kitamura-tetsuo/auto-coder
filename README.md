# Auto-Coder

A Python application that automates application development using AI CLI backends (default: codex, configurable fallback order with `--backend` flag to support gemini/qwen/auggie/codex-mcp). It fetches issues and error PRs from GitHub to build and fix, and automatically creates feature enhancement issues as needed.

## Features

### 🔧 Core Features
- **GitHub API Integration**: Automatic retrieval and management of issues and PRs
- **AI Analysis (codex by default / configurable fallback to Gemini/Qwen/Auggie/codex-mcp via multiple `--backend` flags)**: Automatic analysis of issue and PR content
- **Automated Processing**: Automatic actions based on analysis results
- **Feature Suggestions**: Automatic suggestion of new features based on repository analysis
- **Report Generation**: Detailed reports of processing results

### 🚀 Automated Workflow
1. **Issue Processing**: Retrieve open issues and analyze with AI
2. **PR Processing**: Retrieve open PRs and evaluate risk levels
3. **Feature Suggestions**: Suggest new features from repository context
4. **Automated Actions**: Add comments or close automatically based on analysis results

## Installation

### Prerequisites
- Python 3.9 or higher
- [gh CLI](https://cli.github.com/) pre-authenticated (`gh auth login`)
- [Codex CLI](https://github.com/openai/codex) installed (default backend)
- [Gemini CLI](https://ai.google.dev/gemini-api/docs/cli) required when using Gemini backend (`gemini login`)

### Setup

1. Clone the repository:
```bash
git clone https://github.com/your-username/auto-coder.git
cd auto-coder
```

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync
```

2. Install dependencies to make it executable from any directory:
```bash
source ./venv/bin/activate
pip install -e .
# or install directly without cloning the repository
pip install git+https://github.com/your-username/auto-coder.git
```

> Note (PEP 668 avoidance/recommended): In environments where system Python is externally-managed and `pip install` is blocked, we recommend installing via pipx.
>
> Example for Debian/Ubuntu:
>
> ```bash
> sudo apt update && sudo apt install -y pipx
> pipx ensurepath   # Restart/relogin shell if needed
> pipx install git+https://github.com/kitamura-tetsuo/auto-coder.git
> auto-coder --help
> ```


3. Create configuration file if needed:
```bash
cp .env.example .env
# Tokens can be left empty as gh/gemini authentication is used automatically
```

## Usage

### Authentication

Simply run `gh auth login`. When using the Gemini backend, run `gemini login` to use without setting API keys in environment variables (--model is ignored for codex backend).


#### Using Qwen (Authentication)
- Qwen OAuth (recommended):
  - Run `qwen` once and authenticate your qwen.ai account in the browser to enable automatic use.
  - You can switch to Qwen OAuth by running the `/auth` command mid-process.
  - Reference: Qwen Code official repository (Authorization section): https://github.com/QwenLM/qwen-code
- Automatic fallback when limits are reached:
  - Auto-Coder prioritizes configured OpenAI-compatible endpoints and only falls back to Qwen OAuth when all API keys are exhausted.
  - Configuration file location: `~/.auto-coder/qwen-providers.toml` (path can be overridden with `AUTO_CODER_QWEN_CONFIG`, directory can be specified with `AUTO_CODER_CONFIG_DIR`).
  - TOML example:

    ```toml
    [[qwen.providers]]
    # Option 1: Alibaba Cloud ModelStudio
    name = "modelstudio"
    api_key = "dashscope-..."  # Set the API key you obtained
    # base_url and model default to dashscope-compatible / qwen3-coder-plus

    [[qwen.providers]]
    # Option 2: OpenRouter Free Tier
    name = "openrouter"
    api_key = "openrouter-..."
    model = "qwen/qwen3-coder:free"  # Use default if omitted
    ```

  - Falls back in the order written (API key → OAuth). If only API key is provided, default URL/model is applied, and `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` are automatically injected at runtime.
- OpenAI-compatible mode:
  - Use by setting the following environment variables:
    - `OPENAI_API_KEY` (required)
    - `OPENAI_BASE_URL` (specify according to provider)
    - `OPENAI_MODEL` (e.g., `qwen3-coder-plus`)
  - This tool's Qwen backend uses `qwen -p/--prompt` in non-interactive mode, and models follow `--model/-m` flag or `OPENAI_MODEL` (--model takes priority if both are specified).

#### Using Auggie
- Install CLI with `npm install -g @augmentcode/auggie`.
- This tool calls `auggie --print --model <model_name> "<prompt>"` in non-interactive mode.
- Auggie backend calls are limited to 20 per day. After the 21st call, it automatically stops until the date changes and falls back to other backends.
- If `--model` is not specified, `GPT-5` is used as the default model. Override with `--model` option to specify any model.

### CLI Commands

#### Processing Issues and PRs
```bash
# Run with default (codex backend)
auto-coder process-issues --repo owner/repo

# Switch backend to gemini with model specification
auto-coder process-issues --repo owner/repo --backend gemini --model-gemini gemini-2.5-pro

# Switch backend to qwen with model specification (example: qwen3-coder-plus)
auto-coder process-issues --repo owner/repo --backend qwen --model-qwen qwen3-coder-plus

# Switch backend to auggie (defaults to GPT-5)
auto-coder process-issues --repo owner/repo --backend auggie

# Set codex as default, Gemini as fallback
auto-coder process-issues --repo owner/repo --backend codex --backend gemini

# Run in dry-run mode (no changes made)
auto-coder process-issues --repo owner/repo --dry-run

# Process only specific Issue/PR (by number)
auto-coder process-issues --repo owner/repo --only 123

# Process only specific PR (by URL)
auto-coder process-issues --repo owner/repo --only https://github.com/owner/repo/pull/456
```

#### Creating Feature Suggestion Issues
```bash
# Run with default (codex backend)
auto-coder create-feature-issues --repo owner/repo

# Switch backend to gemini with model specification
auto-coder create-feature-issues --repo owner/repo --backend gemini --model-gemini gemini-2.5-pro

# Switch backend to qwen with model specification (example: qwen3-coder-plus)
auto-coder create-feature-issues --repo owner/repo --backend qwen --model-qwen qwen3-coder-plus

# Switch backend to auggie (defaults to GPT-5)
auto-coder create-feature-issues --repo owner/repo --backend auggie

# Set codex as default, Gemini as fallback
auto-coder create-feature-issues --repo owner/repo --backend codex --backend gemini
```

#### Automated Fix Until Tests Pass (fix-to-pass-tests)
Runs local tests and iteratively requests minimal fixes from LLM when tests fail, then re-runs. Stops with error if LLM makes no edits at all.

```bash
# Run with default (codex backend)
auto-coder fix-to-pass-tests

# Switch backend to gemini with model specification
auto-coder fix-to-pass-tests --backend gemini --model-gemini gemini-2.5-pro

# Switch backend to qwen with model specification (example: qwen3-coder-plus)
auto-coder fix-to-pass-tests --backend qwen --model-qwen qwen3-coder-plus

# Switch backend to auggie (defaults to GPT-5)
auto-coder fix-to-pass-tests --backend auggie

# Set codex as default, Gemini as fallback
auto-coder fix-to-pass-tests --backend codex --backend gemini

# Specify maximum number of attempts (example: max 5)
auto-coder fix-to-pass-tests --max-attempts 5

# Dry run (no edits, just verify execution flow)
auto-coder fix-to-pass-tests --dry-run
```

### Command Options

#### `process-issues`
- `--repo`: GitHub repository (owner/repo format)
- `--backend`: AI backend to use (codex|codex-mcp|gemini|qwen|auggie). Multiple values create fallback chain, first value is default. Default: codex.
- `--model`: Model specification (valid for Gemini/Qwen/Auggie. Ignored with warning for backend=codex/codex-mcp. Auggie uses GPT-5 when unspecified)
- `--dry-run`: Dry run mode (no changes made)
- `--skip-main-update/--no-skip-main-update`: Toggle whether to merge base branch into PR before attempting fixes when PR checks are failing (default: skip base branch merge).
  - Default: `--skip-main-update` (skip)
  - Specify `--no-skip-main-update` to explicitly merge base branch
- `--ignore-dependabot-prs/--no-ignore-dependabot-prs`: Exclude PRs from Dependabot from processing (default: do not ignore)
- `--only`: Process only specific Issue/PR (URL or number specified)

Options:
- `--github-token`: Manual specification when not using gh CLI authentication
- `--gemini-api-key`: Manual specification when not using CLI authentication for Gemini backend

#### `create-feature-issues`
- `--repo`: GitHub repository (owner/repo format)
- `--backend`: AI backend to use (codex|codex-mcp|gemini|qwen|auggie). Multiple values create fallback chain, first value is default. Default: codex.
- `--model`: Model specification (valid for Gemini/Qwen/Auggie. Ignored with warning for backend=codex/codex-mcp. Auggie uses GPT-5 when unspecified)

Options:
- `--github-token`: Manual specification when not using gh CLI authentication
- `--gemini-api-key`: Manual specification when not using CLI authentication for Gemini backend

#### `fix-to-pass-tests`
- `--backend`: AI backend to use (codex|codex-mcp|gemini|qwen|auggie). Multiple values create fallback chain, first value is default. Default: codex.
- `--model`: Model specification (valid for Gemini/Qwen/Auggie. Ignored with warning for backend=codex/codex-mcp. Auggie uses GPT-5 when unspecified)
- `--gemini-api-key`: Manual specification when not using CLI authentication for Gemini backend
- `--max-attempts`: Maximum number of test fix attempts (defaults to engine value)
- `--dry-run`: Dry run mode (no LLM requests, just verify flow)

Behavior:
- Test execution uses `scripts/test.sh` if it exists, otherwise runs `pytest -q --maxfail=1`.
- For each failure, extracts important parts from error output and requests minimal fixes from LLM.
- After fixes, stages and commits changes. Stops with error if there are no changes at all (`nothing to commit`).

## Configuration

### Environment Variables

| Variable | Description | Default Value | Required |
|----------|-------------|---------------|----------|
| `GITHUB_TOKEN` | GitHub API token (override for gh CLI auth) | - | ❌ |
| `GEMINI_API_KEY` | Gemini API key (override for Gemini CLI auth) | - | ❌ |
| `GITHUB_API_URL` | GitHub API URL | `https://api.github.com` | ❌ |
| `GEMINI_MODEL` | Gemini model to use | `gemini-pro` | ❌ |
| `MAX_ISSUES_PER_RUN` | Maximum issues to process per run | `-1` | ❌ |
| `MAX_PRS_PER_RUN` | Maximum PRs to process per run | `-1` | ❌ |
| `DRY_RUN` | Dry run mode | `false` | ❌ |
| `LOG_LEVEL` | Log level | `INFO` | ❌ |

`MAX_ISSUES_PER_RUN` and `MAX_PRS_PER_RUN` are unlimited by default (`-1`). Specify positive integers to limit processing volume.

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

# View logs
docker compose -f docker-compose.graphrag.yml logs
```

3. Setup GraphRAG MCP Server (optional):
```bash
# Automatic setup (recommended)
# Use bundled custom MCP server (code analysis specialized fork)
auto-coder graphrag setup-mcp

# Manual setup
cd ~/graphrag_mcp
uv sync
uv run main.py
```

**Note**: This MCP server is a custom fork of `rileylemm/graphrag_mcp`, specialized for TypeScript/JavaScript code analysis. See `docs/client-features.yaml` `external_dependencies.graphrag_mcp` section for details.

### GraphRAG Service Verification

We provide scripts to verify Neo4j and Qdrant are working correctly:

```bash
# Test everything (default)
python scripts/check_graphrag_services.py

# Test direct access only
python scripts/check_graphrag_services.py --direct-only

# Test MCP only
python scripts/check_graphrag_services.py --mcp-only
```

This script verifies:
- Direct access to Neo4j (Bolt protocol)
  - Database version verification
  - Node creation and search
  - Relationship creation
  - Path search
- Direct access to Qdrant (HTTP API)
  - Collection creation
  - Vector insertion
  - Similarity search
  - Filtered search
- Access via GraphRAG MCP
  - Docker container status
  - MCP server status
  - Index status

### VS Code Debugging Execution

The `.vscode/launch.json` includes the following debug configurations:

- **Check GraphRAG Services (All)**: Test everything (default)
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

### VS Code Debugging Configuration

The project includes the following debugging configurations:

- **Auto-Coder: Process Issues (Dry Run)**: Execute dry run mode in outliner directory
- **Auto-Coder: Create Feature Issues**: Create feature suggestion issues in outliner directory
- **Auto-Coder: Auth Status**: Check authentication status in outliner directory
- **Auto-Coder: Process Issues (Live)**: Execute actual processing in outliner directory

To start debugging:
1. Press `F5` in VS Code or open "Run and Debug" panel
2. Select one of the above configurations
3. Set breakpoints and step through execution

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
# Format
black src/ tests/

# Import order
isort src/ tests/

# Linter
flake8 src/ tests/

# Type checking
mypy src/
```

## Architecture

### Component Structure

```
src/auto_coder/
├── cli.py              # CLI entry point
├── github_client.py    # GitHub API client
├── gemini_client.py    # Gemini AI client
├── automation_engine.py # Main automation engine
└── config.py          # Configuration management
```

### Data Flow

1. **CLI** → **AutomationEngine** → **GitHubClient** (data acquisition)
2. **AutomationEngine** → **GeminiClient** (AI analysis)
3. **AutomationEngine** → **GitHubClient** (action execution)
4. **AutomationEngine** → **Report Generation**

## Output and Reports

Execution results are saved in JSON format in the `~/.auto-coder/{repository}/` directory:

- `automation_report_*.json`: Automation processing results (saved per repository)
- `jules_automation_report_*.json`: Automation processing results in Jules mode

Example: For `owner/repo` repository, reports are saved in `~/.auto-coder/owner_repo/`.

## Troubleshooting

### Common Issues

1. **GitHub API Rate Limit**: Wait and retry after time has passed
2. **Gemini API Errors**: Verify API key is correctly set
3. **Permission Errors**: Verify GitHub token has appropriate permissions

### Checking Logs

```bash
# Set log level to DEBUG
export LOG_LEVEL=DEBUG
auto-coder process-issues --repo owner/repo --dry-run
```

## License

This project is released under the MIT license. See [LICENSE](LICENSE) file for details.

## Contributing

We welcome pull requests and issue reports. Before contributing, please ensure:

1. Tests pass
2. Code style is consistent
3. New features include appropriate tests

## Support

If you have problems or questions, please create an issue on GitHub.

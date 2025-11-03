# Auto-Coder

A Python application that automates application development using an AI CLI backend (default: `codex`. Multiple `--backend` arguments can be specified to set fallback order among gemini / qwen / auggie / codex-mcp). It retrieves issues and error-related PRs from GitHub to build and fix the application, and automatically creates feature-addition issues when necessary.

## Features

### 🔧 Core Features
- **GitHub API Integration**: Automated retrieval and management of issues and PRs
- **AI Analysis** (default: codex / Multiple `--backend` arguments can set Gemini, Qwen, Auggie, codex-mcp in fallback order): Automatic analysis of issues and PRs
- **Automated Processing**: Automated actions based on analysis results
- **Feature Suggestions**: Automatic proposal of new features through repository analysis
- **Report Generation**: Detailed reports of processing results

### 🚀 Automated Workflow
1. **Issue Processing**: Retrieve open issues and analyze them with AI
2. **PR Processing**: Retrieve open PRs and assess risk levels
3. **Feature Suggestions**: Propose new features from repository context
4. **Automated Actions**: Add comments or auto-close based on analysis results

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

> Note (PEP 668 avoidance/recommendation): In environments where system Python is externally-managed and `pip install` is blocked, we recommend installing via pipx.
>
> Example for Debian/Ubuntu:
>
> ```bash
> sudo apt update && sudo apt install -y pipx
> pipx ensurepath   # Restart/re-login to shell if necessary
> pipx install git+https://github.com/kitamura-tetsuo/auto-coder.git
> auto-coder --help
> ```


3. Create a configuration file if needed:
```bash
cp .env.example .env
# Tokens can be left empty as gh and gemini credentials are used automatically
```

## Usage

### Authentication

Basically, run `gh auth login`. When using Gemini backend, run `gemini login` to use without setting API keys in environment variables (codex backend ignores --model).


#### Using Qwen (Authentication)
- Qwen OAuth (recommended):
  - Run `qwen` once, authenticate your browser with qwen.ai account, and it will be available automatically.
  - Run `/auth` command mid-way to switch to Qwen OAuth.
  - Reference: Qwen Code official repository (Authorization section): https://github.com/QwenLM/qwen-code
- Automatic fallback when limits are reached:
  - Auto-Coder prioritizes configured OpenAI-compatible endpoints and only falls back to Qwen OAuth when all API keys are exhausted.
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
    model = "qwen/qwen3-coder:free"  # Uses default if omitted
    ```

  - Fallback is in the order listed (API key → OAuth). If only API key is filled, default URL/model is applied, and `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` are automatically injected at runtime.
- OpenAI-compatible mode:
  - Use by setting the following environment variables.
    - `OPENAI_API_KEY` (required)
    - `OPENAI_BASE_URL` (specify according to provider)
    - `OPENAI_MODEL` (example: `qwen3-coder-plus`)
  - The Qwen backend of this tool uses non-interactive mode with `qwen -p/--prompt`, and the model follows the `--model/-m` flag or `OPENAI_MODEL` (if both are specified, `--model` takes precedence).

#### Using Auggie
- Install CLI with `npm install -g @augmentcode/auggie`
- This tool calls `auggie --print --model <model_name> "<prompt>"` in non-interactive mode.
- Auggie backend calls are limited to 20 per day. After the 21st call, it automatically stops until the date changes and falls back to other backends.
- If `--model` is not specified, `GPT-5` is used as the default model. You can override with the `--model` option if you want to specify a different model.

### CLI Commands

#### Processing issues and PRs
```bash
# Run with default (codex backend)
auto-coder process-issues --repo owner/repo

# Switch backend to gemini and specify model
auto-coder process-issues --repo owner/repo --backend gemini --model-gemini gemini-2.5-pro

# Switch backend to qwen and specify model (example: qwen3-coder-plus)
auto-coder process-issues --repo owner/repo --backend qwen --model-qwen qwen3-coder-plus

# Switch backend to auggie (uses GPT-5 by default)
auto-coder process-issues --repo owner/repo --backend auggie

# Set codex as default and gemini as fallback
auto-coder process-issues --repo owner/repo --backend codex --backend gemini

# Run in dry-run mode (no changes made)
auto-coder process-issues --repo owner/repo --dry-run

# Process only specific Issue/PR (by number)
auto-coder process-issues --repo owner/repo --only 123

# Process only specific PR (by URL)
auto-coder process-issues --repo owner/repo --only https://github.com/owner/repo/pull/456
```

#### Creating feature suggestion issues
```bash
# Run with default (codex backend)
auto-coder create-feature-issues --repo owner/repo

# Switch backend to gemini and specify model
auto-coder create-feature-issues --repo owner/repo --backend gemini --model-gemini gemini-2.5-pro

# Switch backend to qwen and specify model (example: qwen3-coder-plus)
auto-coder create-feature-issues --repo owner/repo --backend qwen --model-qwen qwen3-coder-plus

# Switch backend to auggie (uses GPT-5 by default)
auto-coder create-feature-issues --repo owner/repo --backend auggie

# Set codex as default and gemini as fallback
auto-coder create-feature-issues --repo owner/repo --backend codex --backend gemini
```

#### Automatic fixing until tests pass (fix-to-pass-tests)
Run local tests, and if they fail, request minimal fixes from LLM and repeat execution. Stops with error if LLM makes no edits at all.

```bash
# Run with default (codex backend)
auto-coder fix-to-pass-tests

# Switch backend to gemini and specify model
auto-coder fix-to-pass-tests --backend gemini --model-gemini gemini-2.5-pro

# Switch backend to qwen and specify model (example: qwen3-coder-plus)
auto-coder fix-to-pass-tests --backend qwen --model-qwen qwen3-coder-plus

# Switch backend to auggie (uses GPT-5 by default)
auto-coder fix-to-pass-tests --backend auggie

# Set codex as default and gemini as fallback
auto-coder fix-to-pass-tests --backend codex --backend gemini

# Specify number of attempts (example: max 5 times)
auto-coder fix-to-pass-tests --max-attempts 5

# Dry run (no edits, only check execution flow)
auto-coder fix-to-pass-tests --dry-run
```

### Command Options

#### `process-issues`
- `--repo`: GitHub repository (owner/repo format)
- `--backend`: AI backend to use (codex|codex-mcp|gemini|qwen|auggie). Multiple specifications set fallback order, with the first being default. Default is codex.
- `--model`: Model specification (valid for Gemini/Qwen/Auggie. Ignored for backend=codex/codex-mcp with warning. Auggie uses GPT-5 when unspecified)
- `--dry-run`: Dry run mode (no changes made)
- `--skip-main-update/--no-skip-main-update`: Switch behavior for whether to merge PR's base branch into PR branch before attempting fixes when PR checks fail (default: skip base branch merge).
  - Default: `--skip-main-update` (skip)
  - Specify `--no-skip-main-update` to explicitly merge base branch
- `--ignore-dependabot-prs/--no-ignore-dependabot-prs`: Exclude Dependabot PRs from processing (default: don't exclude)
- `--only`: Process only specific Issue/PR (by URL or number)

Options:
- `--github-token`: Manual specification when not using gh CLI authentication
- `--gemini-api-key`: Manual specification when not using CLI authentication for Gemini backend

#### `create-feature-issues`
- `--repo`: GitHub repository (owner/repo format)
- `--backend`: AI backend to use (codex|codex-mcp|gemini|qwen|auggie). Multiple specifications set fallback order, with the first being default. Default is codex.
- `--model`: Model specification (valid for Gemini/Qwen/Auggie. Ignored for backend=codex/codex-mcp with warning. Auggie uses GPT-5 when unspecified)

Options:
- `--github-token`: Manual specification when not using gh CLI authentication
- `--gemini-api-key`: Manual specification when not using CLI authentication for Gemini backend

#### `fix-to-pass-tests`
- `--backend`: AI backend to use (codex|codex-mcp|gemini|qwen|auggie). Multiple specifications set fallback order, with the first being default. Default is codex.
- `--model`: Model specification (valid for Gemini/Qwen/Auggie. Ignored for backend=codex/codex-mcp with warning. Auggie uses GPT-5 when unspecified)
- `--gemini-api-key`: Manual specification when not using CLI authentication for Gemini backend
- `--max-attempts`: Maximum number of test fix attempts (uses engine default if omitted)
- `--dry-run`: Dry run mode (don't ask LLM, only check flow)

Behavior:
- Test execution uses `scripts/test.sh` if it exists, otherwise runs `pytest -q --maxfail=1`.
- For each failure, extracts important parts from error output and requests minimal fixes from LLM.
- After fixes, stages and commits changes. Stops with error if there are no changes at all (`nothing to commit`).

## Configuration

### Environment Variables

| Variable Name | Description | Default Value | Required |
|--------------|-------------|---------------|----------|
| `GITHUB_TOKEN` | GitHub API token (override gh CLI auth) | - | ❌ |
| `GEMINI_API_KEY` | Gemini API key (override Gemini CLI auth) | - | ❌ |
| `GITHUB_API_URL` | GitHub API URL | `https://api.github.com` | ❌ |
| `GEMINI_MODEL` | Gemini model to use | `gemini-pro` | ❌ |
| `MAX_ISSUES_PER_RUN` | Maximum issues to process per run | `-1` | ❌ |
| `MAX_PRS_PER_RUN` | Maximum PRs to process per run | `-1` | ❌ |
| `DRY_RUN` | Dry run mode | `false` | ❌ |
| `LOG_LEVEL` | Log level | `INFO` | ❌ |

`MAX_ISSUES_PER_RUN` and `MAX_PRS_PER_RUN` are set to unlimited (`-1`) by default. Specify positive integers if you want to limit the number of items processed.

## GraphRAG Integration (Experimental Feature)

Auto-Coder supports GraphRAG (Graph Retrieval-Augmented Generation) integration using Neo4j and Qdrant.

### Setting up GraphRAG

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

3. Setup GraphRAG MCP server (optional):
```bash
# Automatic setup (recommended)
# Uses bundled custom MCP server (code-analysis专用 fork)
auto-coder graphrag setup-mcp

# Manual setup
cd ~/graphrag_mcp
uv sync
uv run main.py
```

**Note**: This MCP server is a custom fork of `rileylemm/graphrag_mcp` specialized for TypeScript/JavaScript code analysis. See `docs/client-features.yaml` `external_dependencies.graphrag_mcp` section for details.

### Verifying GraphRAG Services

We have prepared a script to verify that Neo4j and Qdrant are working correctly:

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

- **Check GraphRAG Services (All)**: Test everything (default)
- **Check GraphRAG Services (Direct only)**: Test direct access only
- **Check GraphRAG Services (MCP only)**: Test MCP only

### GraphRAG Connection Information

Default connection information:

| Service | URL | Credentials |
|---------|-----|-------------|
| Neo4j (Bolt) | `bolt://localhost:7687` | `neo4j` / `password` |
| Neo4j (HTTP) | `http://localhost:7474` | `neo4j` / `password` |
| Qdrant (HTTP) | `http://localhost:6333` | No auth |
| Qdrant (gRPC) | `http://localhost:6334` | No auth |

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

### Setting up Development Environment

1. Install development dependencies:
```bash
pip install -e ".[dev]"
```

2. Setup pre-commit hooks:
```bash
pre-commit install
```

### VS Code Debug Configuration

The project includes the following debug configurations:

- **Auto-Coder: Process Issues (Dry Run)**: Run dry-run mode in outliner directory
- **Auto-Coder: Create Feature Issues**: Create feature suggestion issues in outliner directory
- **Auto-Coder: Auth Status**: Check authentication status in outliner directory
- **Auto-Coder: Process Issues (Live)**: Run actual processing in outliner directory

To start debugging:
1. Press `F5` in VS Code or open the "Run and Debug" panel
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
# Formatting
black src/ tests/

# Import sorting
isort src/ tests/

# Linting
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

1. **CLI** → **AutomationEngine** → **GitHubClient** (data retrieval)
2. **AutomationEngine** → **GeminiClient** (AI analysis)
3. **AutomationEngine** → **GitHubClient** (action execution)
4. **AutomationEngine** → **Report Generation**

## Output and Reports

Execution results are saved in JSON format in the `~/.auto-coder/{repository}/` directory:

- `automation_report_*.json`: Automation processing results (saved per repository)
- `jules_automation_report_*.json`: Automation processing results in Jules mode

Example: For the `owner/repo` repository, reports are saved in `~/.auto-coder/owner_repo/`.

## Troubleshooting

### Common Issues

1. **GitHub API limits**: Wait some time and rerun if you hit rate limits
2. **Gemini API errors**: Verify API key is set correctly
3. **Permission errors**: Verify GitHub token has appropriate permissions

### Checking Logs

```bash
# Set log level to DEBUG
export LOG_LEVEL=DEBUG
auto-coder process-issues --repo owner/repo --dry-run
```

## License

This project is released under the MIT License. See [LICENSE](LICENSE) file for details.

## Contributing

Pull requests and issue reports are welcome. Before contributing, please ensure:

1. Tests pass
2. Code style is consistent
3. New features include appropriate tests

## Support

If you have problems or questions, please create a GitHub issue.

# Qwen Credential Passing Options

## Overview

QwenClient can pass OpenAI-compatible API keys and base URLs in two ways:

1. **Via Environment Variables (default)**: Set `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `OPENAI_MODEL` as environment variables
2. **Via Command-line Options**: Pass `--api-key`, `--base-url`, and `-m` directly to the qwen CLI

## CLI Options

### `--qwen-use-env-vars` / `--qwen-use-cli-options`

Select the method for passing credentials.

- `--qwen-use-env-vars` (default): Pass credentials via environment variables
- `--qwen-use-cli-options`: Pass credentials via command-line options

**Usage Examples:**

```bash
# Via environment variables (default)
auto-coder process-issues --repo owner/repo --backend qwen \
  --openai-api-key sk-xxx --openai-base-url https://api.example.com

# Via command-line options
auto-coder process-issues --repo owner/repo --backend qwen \
  --openai-api-key sk-xxx --openai-base-url https://api.example.com \
  --qwen-use-cli-options
```

### `--qwen-preserve-env` / `--qwen-clear-env`

Select whether to preserve or clear existing `OPENAI_*` environment variables.

- `--qwen-clear-env` (default): Clear existing environment variables before setting new values
- `--qwen-preserve-env`: Preserve existing environment variables and add new values

**Usage Examples:**

```bash
# Clear existing environment variables (default)
auto-coder process-issues --repo owner/repo --backend qwen \
  --openai-api-key sk-xxx

# Preserve existing environment variables
auto-coder process-issues --repo owner/repo --backend qwen \
  --openai-api-key sk-xxx --qwen-preserve-env
```

## Programmatic Usage

### Initializing QwenClient

```python
from auto_coder.qwen_client import QwenClient

# Default (via environment variables, clear existing env vars)
client = QwenClient(
    model_name="qwen3-coder-plus",
    openai_api_key="sk-xxx",
    openai_base_url="https://api.example.com"
)

# Via command-line options
client = QwenClient(
    model_name="qwen3-coder-plus",
    openai_api_key="sk-xxx",
    openai_base_url="https://api.example.com",
    use_env_vars=False  # Use CLI options
)

# Preserve existing environment variables
client = QwenClient(
    model_name="qwen3-coder-plus",
    openai_api_key="sk-xxx",
    openai_base_url="https://api.example.com",
    preserve_existing_env=True  # Preserve existing environment variables
)
```

## Behavioral Differences

### Via Environment Variables (default)

```bash
# Executed command
OPENAI_API_KEY=sk-xxx OPENAI_BASE_URL=https://api.example.com OPENAI_MODEL=qwen3-coder-plus \
  qwen -y -m qwen3-coder-plus -p "prompt text"
```

### Via Command-line Options

```bash
# Executed command
qwen -y --api-key sk-xxx --base-url https://api.example.com -m qwen3-coder-plus -p "prompt text"
```

## Troubleshooting

### When Environment Variables Are Not Passed Correctly

If credentials are not passed correctly via environment variables, try using command-line options:

```bash
auto-coder process-issues --repo owner/repo --backend qwen \
  --openai-api-key sk-xxx --openai-base-url https://api.example.com \
  --qwen-use-cli-options
```

### When Conflicting with Existing Environment Variables

If there is a conflict with existing `OPENAI_*` environment variables, use `--qwen-clear-env` (default) to clear existing environment variables before setting new values.

## Related Files

- `src/auto_coder/qwen_client.py`: QwenClient implementation
- `src/auto_coder/cli_commands_main.py`: CLI command implementation
- `src/auto_coder/cli_helpers.py`: Backend manager construction
- `tests/test_qwen_client_cli_options.py`: Tests for new options


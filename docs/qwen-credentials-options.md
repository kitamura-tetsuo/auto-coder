# Qwen Credential Passing Options

## Overview

QwenClient can pass OpenAI-compatible API keys and base URLs in two ways:

1. **Via Environment Variables (default)**: Set `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL` as environment variables
2. **Via Command Line Options**: Pass `--api-key`, `--base-url`, `-m` directly to qwen CLI

## CLI Options

### `--qwen-use-env-vars` / `--qwen-use-cli-options`

Selects how to pass credentials.

- `--qwen-use-env-vars` (default): Pass credentials via environment variables
- `--qwen-use-cli-options`: Pass credentials via command line options

**Usage Examples:**

```bash
# Via environment variables (default)
auto-coder process-issues --repo owner/repo --backend qwen \
  --openai-api-key sk-xxx --openai-base-url https://api.example.com

# Via command line options
auto-coder process-issues --repo owner/repo --backend qwen \
  --openai-api-key sk-xxx --openai-base-url https://api.example.com \
  --qwen-use-cli-options
```

### `--qwen-preserve-env` / `--qwen-clear-env`

Selects whether to preserve existing `OPENAI_*` environment variables or clear them.

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

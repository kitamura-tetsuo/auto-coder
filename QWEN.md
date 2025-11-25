# Qwen Configuration Guide

This guide explains how to configure Qwen for use with Auto-Coder. There are two primary configuration methods:

1. **Qwen CLI (OAuth)** - Native Qwen CLI authentication
2. **OpenAI-Compatible Providers** - Using Qwen models through providers like OpenRouter, Azure OpenAI, etc.

## Quick Start

### Option 1: Qwen CLI (OAuth) - Direct Qwen Usage

Configure using `backend_type = "qwen"`:

```toml
[backends.qwen]
enabled = true
model = "qwen3-coder-plus"
backend_type = "qwen"

# No API keys needed - uses OAuth
```

### Option 2: OpenRouter (Recommended) - Qwen via OpenAI-Compatible API

Configure using `backend_type = "codex"`:

```toml
[backends.qwen-openrouter]
enabled = true
model = "qwen/qwen3-coder:free"
openai_api_key = "sk-or-v1-your-key-here"
openai_base_url = "https://openrouter.ai/api/v1"
backend_type = "codex"
```

## Configuration Details

### Method 1: Qwen CLI (OAuth)

This method uses the native Qwen CLI with OAuth authentication. No API keys are required.

#### Advantages:
- Simple setup - no API keys to manage
- Native Qwen CLI experience
- Direct integration with Qwen services

#### Setup:
1. Install and authenticate the Qwen CLI
2. Configure Auto-Coder with `backend_type = "qwen"`

```toml
[backends.qwen]
enabled = true
model = "qwen3-coder-plus"
backend_type = "qwen"
```

#### Usage:
```bash
auto-coder process-issues --repo owner/repo --backend qwen
```

### Method 2: OpenAI-Compatible Providers (OpenRouter, Azure, etc.)

This method uses OpenAI-compatible API endpoints to access Qwen models through providers like OpenRouter, Azure OpenAI, or compatible services.

#### Advantages:
- Access to multiple Qwen model variants
- Unified OpenAI-compatible interface
- Better rate limiting and availability

#### Setup:

**For OpenRouter:**

```toml
[backends.qwen-openrouter]
enabled = true
model = "qwen/qwen3-coder:free"
openai_api_key = "sk-or-v1-your-openrouter-key"
openai_base_url = "https://openrouter.ai/api/v1"
backend_type = "codex"

# Optional: Add custom headers
options = ["-o", "HTTPReferer", "https://yourapp.com", "-o", "XTitle", "Auto-Coder"]
```

**For Azure OpenAI:**

```toml
[backends.qwen-azure]
enabled = true
model = "qwen-35-coder"
openai_api_key = "your-azure-openai-key"
openai_base_url = "https://your-resource.openai.azure.com"
backend_type = "codex"

# Azure-specific options
options = ["-o", "api_version", "2024-02-01"]
```

**For Custom OpenAI-Compatible Endpoint:**

```toml
[backends.qwen-custom]
enabled = true
model = "qwen-2.5-coder-32k-instruct"
openai_api_key = "your-api-key"
openai_base_url = "https://api.example.com/v1"
backend_type = "codex"
```

#### Usage:
```bash
# Use with backend name
auto-coder process-issues --repo owner/repo --backend qwen-openrouter

# Or add to backend order
[backend]
order = ["qwen-openrouter", "gemini", "codex"]
default = "qwen-openrouter"
```

## Key Configuration Parameters

### `backend_type`
- `"qwen"`: Use native Qwen CLI (OAuth) - for direct Qwen usage
- `"codex"`: Use OpenAI-compatible client (API keys required) - for providers like OpenRouter

### `openai_api_key`
- API key for your provider (OpenRouter, Azure, etc.)
- Required when `backend_type = "codex"`

### `openai_base_url`
- Provider's API endpoint URL
- Examples:
  - OpenRouter: `https://openrouter.ai/api/v1`
  - Azure: `https://your-resource.openai.azure.com`
  - Custom: `https://api.example.com/v1`

### `model`
- Model name to use
- Examples:
  - OpenRouter: `qwen/qwen3-coder:free`, `qwen/qwen-2.5-coder-32k-instruct`
  - Qwen CLI: `qwen3-coder-plus`

## Complete Configuration Example

Here's a complete `llm_config.toml` example with both Qwen configurations:

```toml
[backend]
default = "qwen-openrouter"
order = ["qwen-openrouter", "qwen", "gemini", "codex"]

[backends.qwen-openrouter]
enabled = true
model = "qwen/qwen3-coder:free"
openai_api_key = "sk-or-v1-your-key-here"
openai_base_url = "https://openrouter.ai/api/v1"
backend_type = "codex"
options = ["-o", "HTTPReferer", "https://yourapp.com", "-o", "XTitle", "Auto-Coder"]

[backends.qwen]
enabled = true
model = "qwen3-coder-plus"
backend_type = "qwen"

[backends.gemini]
enabled = true
model = "gemini-2.5-pro"
api_key = "your-gemini-api-key"
backend_type = "gemini"

[backends.codex]
enabled = true
model = "codex"
backend_type = "codex"
```

## Environment Variables

You can override configuration with environment variables:

```bash
# Global fallback
export AUTO_CODER_OPENAI_API_KEY="sk-or-v1-your-key"
export AUTO_CODER_OPENAI_BASE_URL="https://openrouter.ai/api/v1"

# Backend-specific
export AUTO_CODER_QWEN_OPENAI_API_KEY="sk-or-v1-your-key"
export AUTO_CODER_QWEN_OPENAI_BASE_URL="https://openrouter.ai/api/v1"
```

## Credential Passing Options

Qwen backends support two methods for passing credentials:

### 1. Environment Variables (Default)

Credentials are passed via environment variables:
```bash
export OPENAI_API_KEY="sk-xxx"
export OPENAI_BASE_URL="https://openrouter.ai/api/v1"
export OPENAI_MODEL="qwen/qwen3-coder:free"
```

### 2. Command-Line Options

Credentials are passed directly to the CLI:
```bash
qwen --api-key sk-xxx --base-url https://openrouter.ai/api/v1 -m qwen/qwen3-coder:free -p "prompt"
```

**Switch between methods:**
```bash
# Use environment variables (default)
auto-coder process-issues --repo owner/repo --backend qwen-openrouter

# Use command-line options
auto-coder process-issues --repo owner/repo --backend qwen-openrouter --qwen-use-cli-options
```

**Environment variable management:**
```bash
# Clear existing environment variables (default)
auto-coder process-issues --repo owner/repo --backend qwen-openrouter

# Preserve existing environment variables
auto-coder process-issues --repo owner/repo --backend qwen-openrouter --qwen-preserve-env
```

## Which Method to Choose?

| Use Case | Recommended Method | `backend_type` |
|----------|-------------------|----------------|
| Direct Qwen CLI access | Qwen CLI (OAuth) | `"qwen"` |
| OpenRouter access | OpenAI-Compatible | `"codex"` |
| Azure OpenAI | OpenAI-Compatible | `"codex"` |
| Custom OpenAI-compatible endpoint | OpenAI-Compatible | `"codex"` |
| Multiple provider options | OpenAI-Compatible | `"codex"` |

## Troubleshooting

### "qwen CLI not available" Error
- Install and authenticate the Qwen CLI
- Ensure Qwen CLI is in your PATH
- Verify with: `qwen --version`

### "Backend type 'qwen' not supported" Error
- Check that `backend_type` is set correctly
- For Qwen CLI: `backend_type = "qwen"`
- For OpenAI-compatible: `backend_type = "codex"`

### Authentication Errors with OpenRouter/Azure
- Verify your API key is correct
- Check the base URL format
- Ensure the model name is valid for your provider
- Verify the API key has sufficient permissions

### Rate Limit Errors
- Provider-specific rate limits apply
- Consider switching to a different model or provider
- Configure retry settings in your backend

## Migration from Old Configuration

If you have an existing configuration, here's what changed:

**Old behavior:**
- QwenClient could fall back to Codex internally
- Single configuration method

**New behavior:**
- Must explicitly choose between:
  - `backend_type = "qwen"` for Qwen CLI
  - `backend_type = "codex"` for OpenAI-compatible providers
- No automatic fallback between different backend types

## Additional Resources

- [LLM Backend Configuration Reference](docs/llm_backend_config.example.toml)
- [OpenRouter Setup Guide](OPENROUTER_SETUP.md)
- [Qwen Credential Options](docs/qwen-credentials-options.md)
- [Backend Manager Documentation](GLOBAL_BACKEND_MANAGER_USAGE.md)

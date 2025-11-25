# OpenRouter Setup Guide

## Overview

OpenRouter provides access to various AI models through a unified OpenAI-compatible API. This guide explains how to configure Auto-Coder to use Qwen and other models via OpenRouter.

**Key Point:** When using OpenRouter with Auto-Coder, configure your backend with `backend_type = "codex"` to use the OpenAI-compatible client.

## Quick Start

### 1. Get OpenRouter API Key

1. Sign up at [OpenRouter.ai](https://openrouter.ai/)
2. Navigate to the [Keys page](https://openrouter.ai/keys)
3. Create a new API key

### 2. Configure Auto-Coder

Create or update `~/.auto-coder/llm_config.toml`:

```toml
[backend]
default = "qwen-openrouter"
order = ["qwen-openrouter", "codex", "gemini"]

[backends.qwen-openrouter]
enabled = true
model = "qwen/qwen3-coder:free"
openai_api_key = "sk-or-v1-your-key-here"
openai_base_url = "https://openrouter.ai/api/v1"
backend_type = "codex"

# Optional: Add custom headers for tracking
options = ["-o", "HTTPReferer", "https://yourapp.com", "-o", "XTitle", "Auto-Coder"]
```

### 3. Verify Configuration

```bash
auto-coder process-issues --repo owner/repo --backend qwen-openrouter --dry-run
```

## Configuration Details

### Backend Configuration

The key is setting `backend_type = "codex"` to use Auto-Coder's OpenAI-compatible client:

```toml
[backends.qwen-openrouter]
enabled = true
model = "qwen/qwen3-coder:free"
openai_api_key = "sk-or-v1-your-key-here"
openai_base_url = "https://openrouter.ai/api/v1"
backend_type = "codex"
```

### Important Parameters

- **`backend_type = "codex"`**: Use OpenAI-compatible client (required for OpenRouter)
- **`openai_api_key`**: Your OpenRouter API key
- **`openai_base_url`**: Must be `https://openrouter.ai/api/v1`
- **`model`**: OpenRouter model identifier (e.g., `qwen/qwen3-coder:free`)

### Available Qwen Models on OpenRouter

Common Qwen models available through OpenRouter:

- `qwen/qwen3-coder:free` - Free tier Qwen 3 Coder
- `qwen/qwen-2.5-coder-32k-instruct` - Qwen 2.5 Coder (32K context)
- `qwen/qwen-2.5-coder-72b-instruct` - Qwen 2.5 Coder (72B parameters)
- `qwen/qwen-2.5-plus` - Qwen 2.5 Plus (general purpose)

Visit [OpenRouter Models](https://openrouter.ai/models) for the complete list and pricing.

## Multiple Provider Configuration

You can configure multiple OpenRouter backends with different models:

```toml
[backends.qwen-openrouter-free]
enabled = true
model = "qwen/qwen3-coder:free"
openai_api_key = "sk-or-v1-your-key-here"
openai_base_url = "https://openrouter.ai/api/v1"
backend_type = "codex"

[backends.qwen-openrouter-plus]
enabled = true
model = "qwen/qwen-2.5-plus"
openai_api_key = "sk-or-v1-your-key-here"
openai_base_url = "https://openrouter.ai/api/v1"
backend_type = "codex"

[backend]
order = ["qwen-openrouter-free", "qwen-openrouter-plus", "gemini"]
default = "qwen-openrouter-free"
```

## Custom Headers (Optional)

OpenRouter supports custom headers for tracking usage:

```toml
options = ["-o", "HTTPReferer", "https://yourapp.com", "-o", "XTitle", "Auto-Coder"]
```

These are passed to the OpenAI-compatible API to help identify your application.

## Environment Variable Configuration

Alternatively, you can use environment variables:

```bash
export AUTO_CODER_OPENAI_API_KEY="sk-or-v1-your-key-here"
export AUTO_CODER_OPENAI_BASE_URL="https://openrouter.ai/api/v1"
```

## Usage Examples

### Process Issues with Qwen via OpenRouter

```bash
# Use specific OpenRouter backend
auto-coder process-issues --repo owner/repo --backend qwen-openrouter

# Use backend order
auto-coder process-issues --repo owner/repo
```

### Feature Suggestion with OpenRouter

```bash
auto-coder create-feature-issues --repo owner/repo --backend qwen-openrouter
```

### Fix Issues with OpenRouter

```bash
auto-coder fix-to-pass-tests --target-repository owner/repo --backend qwen-openrouter
```

## Troubleshooting

### "Invalid API key" Error

1. Verify your OpenRouter API key is correct
2. Ensure the key hasn't expired
3. Check your OpenRouter account has sufficient credits

### "Model not found" Error

1. Verify the model name is correct
2. Check the model is available on OpenRouter
3. Ensure your API key has access to the model (some models require special access)

### Rate Limit Errors

OpenRouter has rate limits depending on your plan:

- **Free tier**: Limited requests per minute/day
- **Paid tier**: Higher limits available

If you hit rate limits:
- Wait before retrying
- Use a free model like `qwen/qwen3-coder:free` for testing
- Consider upgrading your OpenRouter plan

### "Invalid base URL" Error

Ensure `openai_base_url` is set to exactly:
```
https://openrouter.ai/api/v1
```

## Migration from Codex Fallback

**Previous behavior:**
- QwenClient had an internal Codex fallback mechanism

**Current behavior:**
- Must explicitly configure OpenRouter backends with `backend_type = "codex"`
- No automatic fallback between backend types

**Migration steps:**
1. Get your OpenRouter API key
2. Create a backend configuration with `backend_type = "codex"`
3. Remove any reliance on automatic fallback behavior
4. Test with your new configuration

## Comparison: Qwen CLI vs OpenRouter

| Feature | Qwen CLI (OAuth) | OpenRouter |
|---------|------------------|------------|
| Setup | Requires Qwen CLI auth | Requires API key only |
| Model Access | Limited to Qwen CLI models | Access to all OpenRouter models |
| Pricing | Varies by provider | Transparent per-model pricing |
| Integration | Native CLI tool | OpenAI-compatible API |
| Setup Complexity | Moderate | Simple |
| Rate Limits | Provider-specific | OpenRouter's limits |

## Additional Resources

- [OpenRouter Website](https://openrouter.ai/)
- [OpenRouter Models](https://openrouter.ai/models)
- [OpenRouter API Documentation](https://openrouter.ai/docs)
- [Qwen Configuration Guide](QWEN.md)
- [LLM Backend Configuration](docs/llm_backend_config.example.toml)

## Best Practices

1. **Use environment variables** for API keys in production
2. **Start with free models** for testing (`qwen/qwen3-coder:free`)
3. **Monitor your usage** through the OpenRouter dashboard
4. **Configure multiple backends** for fallback options
5. **Set appropriate rate limits** in your OpenRouter account settings
6. **Use custom headers** for better tracking and billing attribution

# Qwen Backend Configuration

## Overview

The Qwen backend now supports the shared provider management system, allowing you to configure multiple providers for the same model and benefit from automatic provider rotation and environment variable handling.

## Provider Management System

### What is Provider Management?

Provider management allows you to define multiple provider implementations for the same backend (e.g., Qwen). Each provider represents a different way to access the Qwen model:

- **Direct API access**: Connect directly to Qwen's API
- **Via OpenRouter**: Use OpenRouter's managed access to Qwen
- **Via Azure OpenAI**: Use Azure's OpenAI-compatible endpoint for Qwen

### Benefits

- **Automatic failover**: When one provider hits usage limits, automatically switch to the next
- **Environment variable handling**: Provider-specific settings are automatically exported and cleaned up
- **Telemetry**: Provider information is tracked for debugging and monitoring
- **Graceful degradation**: Works without providers configured, falling back to standard behavior

## Configuration

### Step 1: Configure Backend with Provider List

Edit `~/.auto-coder/llm_config.toml`:

```toml
[backends.qwen]
enabled = true
model = "qwen3-coder-plus"
providers = ["qwen-open-router", "qwen-azure", "qwen-direct"]
```

### Step 2: Define Provider Metadata

Create or edit `~/.auto-coder/provider_metadata.toml`:

```toml
[qwen.qwen-open-router]
command = "uvx"
args = ["qwen-openai-proxy"]
description = "Qwen via OpenRouter API"
OPENROUTER_API_KEY = "your-openrouter-key"

[qwen.qwen-azure]
command = "uvx"
args = ["qwen-azure-proxy"]
description = "Qwen via Azure OpenAI"
AZURE_ENDPOINT = "https://your-endpoint.openai.azure.com"
AZURE_API_VERSION = "2024-02-15-preview"
AZURE_API_KEY = "your-azure-key"

[qwen.qwen-direct]
command = "uvx"
args = ["qwen-direct"]
description = "Direct Qwen API access"
QWEN_API_KEY = "your-qwen-api-key"
```

### Provider Metadata Schema

Each provider definition in `provider_metadata.toml` supports:

- **command**: The command to execute (e.g., "uvx")
- **args**: List of arguments for the command (e.g., ["qwen-openai-proxy"])
- **description**: Human-readable description of the provider
- **Uppercase settings**: Any uppercase keys are exported as environment variables

### Common Provider Configurations

#### OpenRouter Provider

```toml
[qwen.qwen-open-router]
command = "uvx"
args = ["qwen-openai-proxy"]
description = "Qwen via OpenRouter API"
OPENROUTER_API_KEY = "sk-or-v1-your-key"
```

#### Azure OpenAI Provider

```toml
[qwen.qwen-azure]
command = "uvx"
args = ["qwen-azure-proxy"]
description = "Qwen via Azure OpenAI"
AZURE_ENDPOINT = "https://your-resource.openai.azure.com"
AZURE_API_VERSION = "2024-02-15-preview"
AZURE_API_KEY = "your-azure-key"
```

#### Direct Qwen API Provider

```toml
[qwen.qwen-direct]
command = "uvx"
args = ["qwen-direct"]
description = "Direct Qwen API access"
QWEN_API_KEY = "your-qwen-key"
```

## Environment Variable Handling

### Automatic Export

Uppercase settings from provider metadata are automatically exported as environment variables during command execution:

```toml
[qwen.qwen-azure]
AZURE_ENDPOINT = "https://your-endpoint.openai.azure.com"
AZURE_API_KEY = "your-key"
```

During Qwen execution, these variables are automatically set:
- `AZURE_ENDPOINT` = "https://your-endpoint.openai.azure.com"
- `AZURE_API_KEY` = "your-key"

### Automatic Cleanup

Environment variables are automatically cleared once the command completes, ensuring:
- No leftover environment state
- Clean execution environment for subsequent commands
- Proper isolation between provider executions

### Priority

Provider environment variables take precedence over system environment variables during execution but don't modify your system's permanent environment.

## Provider Rotation

### Automatic Rotation

Provider rotation happens automatically when:
- A provider hits usage limits (AutoCoderUsageLimitError)
- You want to try a different provider

### Manual Rotation

You can manually control provider rotation:

```python
from auto_coder.backend_manager import get_llm_backend_manager

manager = get_llm_backend_manager(
    default_backend="qwen",
    default_client=client,
    factories={"qwen": lambda: client}
)

# Advance to next provider
provider_manager = manager.provider_manager
rotated = provider_manager.advance_to_next_provider("qwen")

# Reset to first provider
provider_manager.reset_provider_rotation("qwen")
```

### Rotation Order

Providers are rotated in the order they appear in the `providers` list in `llm_config.toml`:

```toml
providers = ["qwen-open-router", "qwen-azure", "qwen-direct"]
# Rotation order: open-router → azure → direct → open-router → ...
```

## Status Monitoring

### Get Current Provider

```python
from auto_coder.backend_manager import get_llm_backend_manager

manager = get_llm_backend_manager()

# Get current provider name
current_provider = manager.provider_manager.get_current_provider_name("qwen")
print(f"Current provider: {current_provider}")

# Get backend, provider, and model info
backend, provider, model = manager.get_last_backend_provider_and_model()
print(f"Backend: {backend}, Provider: {provider}, Model: {model}")
```

### Provider Information

```python
# Check if providers are configured
has_providers = manager.provider_manager.has_providers("qwen")
print(f"Providers configured: {has_providers}")

# Get all provider names
provider_names = manager.provider_manager.get_all_provider_names("qwen")
print(f"Available providers: {provider_names}")

# Get provider count
provider_count = manager.provider_manager.get_provider_count("qwen")
print(f"Total providers: {provider_count}")

# Get last used provider
last_provider = manager.provider_manager.get_last_used_provider_name("qwen")
print(f"Last used: {last_provider}")
```

## Migration from Old Qwen-Only Provider System

### What Changed

Previously, Qwen had its own provider configuration system using `qwen-providers.toml`. This has been replaced by the shared provider management system that works across all backends.

### Migration Steps

1. **Old system**: `~/.auto-coder/qwen-providers.toml`
2. **New system**: `~/.auto-coder/provider_metadata.toml`

**Before (old system):**
```toml
# qwen-providers.toml
[modelstudio]
base_url = "..."
model = "..."
```

**After (new system):**
```toml
# provider_metadata.toml
[qwen.modelstudio]
command = "..."
args = [...]
base_url = "..."
model = "..."
```

### Key Differences

- **File location**: Changed from `qwen-providers.toml` to `provider_metadata.toml`
- **Section names**: Now use `[qwen.<provider-name>]` instead of `[<provider-name>]`
- **Shared system**: Now works with all backends (Qwen, Gemini, Claude, etc.)
- **Environment handling**: Automatic export/cleanup of uppercase settings
- **Status APIs**: New methods for monitoring provider state

## Without Providers (Fallback Mode)

The system works without providers configured:

```toml
# llm_config.toml - No providers configured
[backends.qwen]
enabled = true
model = "qwen3-coder-plus"
# providers = []  # Not specified
```

In this mode:
- Standard Qwen backend behavior is used
- No automatic provider rotation
- Environment variables from provider metadata are not available
- Graceful degradation to existing functionality

## Multiple Providers for Same Model

You can configure multiple providers for the same Qwen model:

```toml
# llm_config.toml
[backends.qwen]
enabled = true
model = "qwen3-coder-plus"
providers = [
    "qwen-open-router",  # Try OpenRouter first
    "qwen-azure",        # Fall back to Azure
    "qwen-direct"        # Finally try direct API
]
```

Each provider can have different:
- API endpoints
- Authentication methods
- Rate limiting characteristics
- Pricing structures

## Examples

### Complete Example: Multiple Qwen Providers

**llm_config.toml:**
```toml
[backends.qwen]
enabled = true
model = "qwen3-coder-plus"
providers = ["qwen-open-router", "qwen-azure", "qwen-direct"]

[message_backend]
default = "qwen"
order = ["qwen", "gemini"]
```

**provider_metadata.toml:**
```toml
# Primary provider: OpenRouter (free tier)
[qwen.qwen-open-router]
command = "uvx"
args = ["qwen-openai-proxy"]
description = "Qwen via OpenRouter (free tier)"
OPENROUTER_API_KEY = "${OPENROUTER_API_KEY}"

# Secondary provider: Azure OpenAI
[qwen.qwen-azure]
command = "uvx"
args = ["qwen-azure-proxy"]
description = "Qwen via Azure OpenAI"
AZURE_ENDPOINT = "${AZURE_ENDPOINT}"
AZURE_API_VERSION = "2024-02-15-preview"
AZURE_API_KEY = "${AZURE_API_KEY}"

# Tertiary provider: Direct API
[qwen.qwen-direct]
command = "uvx"
args = ["qwen-direct"]
description = "Direct Qwen API"
QWEN_API_KEY = "${QWEN_API_KEY}"
```

### Using Environment Variables

Set environment variables in your shell:

```bash
export OPENROUTER_API_KEY="sk-or-v1-your-key"
export AZURE_ENDPOINT="https://your-resource.openai.azure.com"
export AZURE_API_KEY="your-azure-key"
export QWEN_API_KEY="your-qwen-key"
```

Or use them directly in `provider_metadata.toml`:

```toml
[qwen.qwen-open-router]
OPENROUTER_API_KEY = "${OPENROUTER_API_KEY}"
```

## Troubleshooting

### Provider Not Found

```
Error: No providers configured for backend 'qwen'
```

**Solution**: Add `providers` list to `[backends.qwen]` in `llm_config.toml`

### Provider Metadata File Not Found

```
Warning: provider_metadata.toml not found
```

**Solution**: Create `~/.auto-coder/provider_metadata.toml` and define your providers

### Environment Variable Not Set

```
Error: AZURE_ENDPOINT not set
```

**Solution**: Ensure uppercase settings in provider metadata are properly defined with values

### Provider Rotation Not Working

**Check**:
1. Multiple providers are configured in `llm_config.toml`
2. Providers are defined in `provider_metadata.toml`
3. You're hitting usage limits (AutoCoderUsageLimitError)

## Additional Resources

- **Global Backend Manager Usage**: See `GLOBAL_BACKEND_MANAGER_USAGE.md` for complete API reference
- **Configuration Examples**: See `docs/llm_backend_config.example.toml`
- **Provider Manager API**: See `src/auto_coder/backend_provider_manager.py`

## Summary

The Qwen backend now uses the shared provider management system, providing:
- ✅ Multiple provider configurations per model
- ✅ Automatic provider rotation on usage limits
- ✅ Environment variable export and cleanup
- ✅ Status monitoring and telemetry
- ✅ Graceful degradation without providers
- ✅ Migration path from old system

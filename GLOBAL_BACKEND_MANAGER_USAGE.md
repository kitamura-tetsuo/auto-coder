# Global Backend Manager Usage

## Overview

This enables the LLM backend manager to be used globally as a singleton from anywhere. The backend manager provides provider-aware backend management with automatic provider rotation, environment variable handling, and usage tracking.

## Available Global Functions

### Main LLM Operations

```python
from auto_coder.backend_manager import get_llm_backend_manager, run_llm_prompt, get_llm_backend_and_model

# Get LLM backend (initialization required only once)
manager = get_llm_backend_manager(
    default_backend="codex",
    default_client=client,
    factories={"codex": lambda: client}
)

# Execute prompt
response = run_llm_prompt("your prompt here")

# Get latest backend information
backend, model = get_llm_backend_and_model()

# Get backend information including provider
backend, provider, model = get_llm_backend_provider_and_model()
print(f"Using {backend} with provider {provider} and model {model}")
```

## Provider Management Configuration

### Configuration Files

Provider management uses two configuration files:

1. **LLM Backend Configuration** (`~/.auto-coder/llm_config.toml`)
   - Declares which providers to use for each backend
   - Syntax: `providers = ["provider-name-1", "provider-name-2"]`

2. **Provider Metadata** (`~/.auto-coder/provider_metadata.toml`)
   - Defines details for each provider
   - Includes command, args, description, and uppercase settings

### Example Configuration

**llm_config.toml:**
```toml
[backends.qwen]
enabled = true
model = "qwen3-coder-plus"
providers = ["qwen-open-router", "qwen-azure", "qwen-direct"]
```

**provider_metadata.toml:**
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

[qwen.qwen-direct]
command = "uvx"
args = ["qwen-direct"]
description = "Direct Qwen API access"
QWEN_API_KEY = "your-qwen-api-key"
```

### Environment Variable Handling

Uppercase settings from provider metadata are automatically exported as environment variables during command execution:

- **Export**: Provider metadata uppercase settings are set as environment variables when a provider is active
- **Scope**: Environment variables are scoped to the lifetime of the command execution
- **Cleanup**: Variables are automatically cleared once the command completes
- **Priority**: Provider environment variables take precedence over system environment variables during execution

**Example:**
```python
# If provider_metadata.toml defines:
# [qwen.qwen-azure]
# AZURE_ENDPOINT = "https://your-endpoint.openai.azure.com"

# Then during qwen execution, AZURE_ENDPOINT is automatically set
# After execution completes, AZURE_ENDPOINT is automatically unset
```

## LLMBackendManager Class

### Basic Singleton Access

```python
from auto_coder.backend_manager import LLMBackendManager

# Get LLM backend
manager = LLMBackendManager.get_llm_instance(
    default_backend="gemini",
    default_client=client,
    factories={"gemini": lambda: client}
)
```

### Provider Manager Access

```python
# Access the provider manager for status reporting
manager = LLMBackendManager.get_llm_instance(
    default_backend="qwen",
    default_client=client,
    factories={"qwen": lambda: client}
)

# Check if providers are configured
has_providers = manager.provider_manager.has_providers("qwen")
print(f"Providers configured: {has_providers}")

# Get current provider name
current_provider = manager.provider_manager.get_current_provider_name("qwen")
print(f"Current provider: {current_provider}")

# Get all provider names
provider_names = manager.provider_manager.get_all_provider_names("qwen")
print(f"Available providers: {provider_names}")

# Get provider count
provider_count = manager.provider_manager.get_provider_count("qwen")
print(f"Total providers: {provider_count}")
```

## Nested Manager Instances

The system supports multiple backend manager instances with different configurations:

### LLM Backend Manager
- **Purpose**: General LLM operations (PR processing, test fixes, code generation)
- **Access**: `get_llm_backend_manager()` or `LLMBackendManager.get_llm_instance()`
- **Usage**: Primary backend for all LLM operations

### Message Backend Manager
- **Purpose**: Message generation (commit messages, PR messages, etc.)
- **Access**: `get_message_backend_manager()` or `LLMBackendManager.get_message_instance()`
- **Usage**: Separate backend for generating human-readable messages

### Provider Manager
- **Purpose**: Shared across all backend manager instances
- **Tracks**: Provider rotation state, last used providers, metadata cache
- **Access**: Via `manager.provider_manager` property

### Example with Multiple Managers

```python
from auto_coder.backend_manager import (
    get_llm_backend_manager,
    get_message_backend_manager,
    LLMBackendManager
)

# Initialize LLM backend (for code generation)
llm_manager = LLMBackendManager.get_llm_instance(
    default_backend="qwen",
    default_client=qwen_client,
    factories={"qwen": lambda: qwen_client}
)

# Initialize message backend (for commit messages)
message_manager = LLMBackendManager.get_message_instance(
    default_backend="gemini",
    default_client=gemini_client,
    factories={"gemini": lambda: gemini_client}
)

# Both managers share the same provider manager instance
assert llm_manager.provider_manager is message_manager.provider_manager
```

## Status Reporting APIs

### Getting Backend Information

```python
# Get backend and model
backend, model = manager.get_last_backend_and_model()
print(f"Last used backend: {backend}, model: {model}")

# Get backend, provider, and model (recommended)
backend, provider, model = manager.get_last_backend_provider_and_model()
print(f"Last used backend: {backend}, provider: {provider}, model: {model}")
```

### Provider Status Methods

```python
from auto_coder.backend_provider_manager import BackendProviderManager

# Create provider manager
provider_manager = BackendProviderManager.get_default_manager()

# Load provider metadata
provider_manager.load_provider_metadata()

# Check status
print(f"Has providers for 'qwen': {provider_manager.has_providers('qwen')}")
print(f"Provider count for 'qwen': {provider_manager.get_provider_count('qwen')}")
print(f"Current provider for 'qwen': {provider_manager.get_current_provider_name('qwen')}")
print(f"Last used provider for 'qwen': {provider_manager.get_last_used_provider_name('qwen')}")

# Get next provider (for rotation)
next_provider = provider_manager.get_next_provider('qwen')
if next_provider:
    print(f"Next provider: {next_provider.name}")
```

### Environment Context

```python
# Create environment variable context from current provider
env_vars = provider_manager.create_env_context("qwen")
print(f"Environment variables from provider: {env_vars}")

# These environment variables can be used with CommandExecutor or similar tools
# They are automatically scoped to the command execution lifetime
```

## Usage Examples

### Basic Usage Pattern

```python
# 1. Import
from auto_coder.backend_manager import (
    LLMBackendManager,
    get_llm_backend_manager,
    run_llm_prompt,
)

# 2. Initialization (execute only once)
manager = LLMBackendManager.get_llm_instance(
    default_backend="gemini",
    default_client=gemini_client,
    factories={"gemini": lambda: gemini_client}
)

# 3. Usage
response = run_llm_prompt("Generate some code")

# 4. Get backend information including provider
backend, provider, model = manager.get_last_backend_provider_and_model()
print(f"Using {backend} with provider {provider} and model {model}")
```

### Error Handling

```python
try:
    manager = get_llm_backend_manager(
        default_backend="invalid-backend",
        default_client=None,
        factories={}
    )
except RuntimeError as e:
    print(f"Initialization error: {e}")

# Initial call requires parameters
manager = get_llm_backend_manager()  # RuntimeError occurs

# Subsequent calls can be made without parameters
manager = get_llm_backend_manager()  # Returns existing instance
```

### Provider Rotation Example

```python
from auto_coder.backend_manager import get_llm_backend_manager
from auto_coder.exceptions import AutoCoderUsageLimitError

# Initialize with multiple providers
manager = get_llm_backend_manager(
    default_backend="qwen",
    default_client=qwen_client,
    factories={"qwen": lambda: qwen_client}
)

# Provider rotation happens automatically on usage limit errors
try:
    response = run_llm_prompt("Generate code")
except AutoCoderUsageLimitError:
    print("All providers exhausted for this backend")

# Manual provider rotation (if needed)
provider_manager = manager.provider_manager
rotated = provider_manager.advance_to_next_provider("qwen")
if rotated:
    print("Manually rotated to next provider")

# Reset rotation to start from beginning
provider_manager.reset_provider_rotation("qwen")
```

## Thread Safety

All functions are designed to be thread-safe and can be safely accessed from multiple threads simultaneously. The provider manager uses internal locking to ensure thread-safe provider rotation and state tracking.

## Important Notes

1. **Initialization Only Once**: Initialization parameters are required only on the first call
2. **Resource Management**: Please clean up appropriately when the application exits
3. **Configuration Changes**: Configuration can be changed with `force_reinitialize=True`
4. **Backward Compatibility**: Existing `LLMBackendManager.get_llm_instance()` can continue to be used
5. **Provider Metadata**: Provider metadata is optional and the system degrades gracefully if no providers are configured
6. **Environment Variables**: Uppercase settings from provider metadata are automatically exported and cleared during command execution
7. **Provider Rotation**: Rotation state is persisted across calls to maintain consistent retry ordering when usage limits are hit

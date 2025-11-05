# Global Backend Manager Usage

## Overview

This enables the LLM backend manager to be used globally as a singleton from anywhere.

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

# 4. Get backend information
backend, model = get_llm_backend_and_model()
print(f"Using {backend} with model {model}")
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

## Thread Safety

All functions are designed to be thread-safe and can be safely accessed from multiple threads simultaneously.

## Important Notes

1. **Initialization Only Once**: Initialization parameters are required only on the first call
2. **Resource Management**: Please clean up appropriately when the application exits
3. **Configuration Changes**: Configuration can be changed with `force_reinitialize=True`
4. **Backward Compatibility**: Existing `LLMBackendManager.get_llm_instance()` can continue to be used

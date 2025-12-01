# Migration Guide: CLI Options Configuration

**Breaking Change: Transition from Hardcoded CLI Options to Configuration-Based Approach**

## Overview

This migration guide helps users update their configurations from the old hardcoded approach to the new config-based approach. The system has been refactored to use explicit CLI options defined in configuration files instead of hardcoded defaults.

**Breaking Changes Summary:**

- **Config Format**: `message_backend` → `backend_for_noedit`
- **API Functions**: `get_message_backend_manager()` → `get_noedit_backend_manager()`
- **Hardcoded Options**: All CLI options must now be explicitly configured

## Migration Steps

### Step 1: Update Config File Structure

#### Old Configuration (Deprecated)

```toml
[message_backend]
default = "codex"
order = ["codex", "gemini"]

[backends.codex]
model = "codex"
# options were hardcoded in code
```

#### New Configuration (Required)

```toml
[backend_for_noedit]
default = "codex"
order = ["codex", "gemini"]

[backends.codex]
model = "codex"
# All options must be explicit
options = ["--dangerously-bypass-approvals-and-sandbox"]
options_for_noedit = ["--dangerously-bypass-approvals-and-sandbox"]
```

### Step 2: Add Required Options Per Backend

Each backend now requires explicit `options` and `options_for_noedit` configuration. Below are the before/after examples for each backend:

#### Codex Backend

**Before (Hardcoded):**
```toml
[backends.codex]
model = "codex"
# Options were automatically added by the system
```

**After (Explicit Configuration Required):**
```toml
[backends.codex]
model = "codex"
options = ["--dangerously-bypass-approvals-and-sandbox"]
options_for_noedit = ["--dangerously-bypass-approvals-and-sandbox"]
```

#### Claude Backend

**Before (Hardcoded):**
```toml
[backends.claude]
model = "sonnet"
# Options were automatically added by the system
```

**After (Explicit Configuration Required):**
```toml
[backends.claude]
model = "sonnet"
options = ["--print", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"]
options_for_noedit = ["--print", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"]
```

#### Gemini Backend

**Before (Hardcoded):**
```toml
[backends.gemini]
model = "gemini-2.5-pro"
# Options were automatically added by the system
```

**After (Explicit Configuration Required):**
```toml
[backends.gemini]
model = "gemini-2.5-pro"
options = ["--yolo", "--force-model"]
options_for_noedit = ["--yolo", "--force-model"]
```

#### Qwen Backend

**Before (Hardcoded):**
```toml
[backends.qwen]
model = "qwen3-coder-plus"
# Options were automatically added by the system
```

**After (Explicit Configuration Required):**
```toml
[backends.qwen]
model = "qwen3-coder-plus"
options = ["-y"]
options_for_noedit = ["-y"]
```

#### Auggie Backend

**Before (Hardcoded):**
```toml
[backends.auggie]
model = "GPT-5"
# Options were automatically added by the system
```

**After (Explicit Configuration Required):**
```toml
[backends.auggie]
model = "GPT-5"
options = ["--print"]
options_for_noedit = ["--print"]
```

#### Jules Backend

**Before (Hardcoded):**
```toml
[backends.jules]
model = "jules"
# Session-based, no hardcoded options
```

**After (Session-Based Configuration):**
```toml
[backends.jules]
# Session-based, minimal options required
options = []
options_for_noedit = []
```

**Notes:**
- Jules is session-based and doesn't require complex CLI options
- The backend_type should be set to "jules"
- OAuth authentication is used automatically

### Step 3: Update Code References

#### Python API Changes

**Old (Deprecated, still works with warning):**
```python
from auto_coder.backend_manager import get_message_backend_manager

manager = get_message_backend_manager()
response = manager.run_llm_message_prompt("Your prompt")
```

**New (Required):**
```python
from auto_coder.backend_manager import get_noedit_backend_manager

manager = get_noedit_backend_manager()
response = manager.run_llm_message_prompt("Your prompt")
```

**Migration Script:**

If you're using the old API, you can use this migration script:

```python
import warnings

def migrate_from_message_backend():
    """Migrate from message_backend to backend_for_noedit API."""
    warnings.warn(
        "get_message_backend_manager() is deprecated. "
        "Use get_noedit_backend_manager() instead. "
        "This will be removed in a future version.",
        DeprecationWarning,
        stacklevel=2
    )
    # Continue to work with the old function
    from auto_coder.backend_manager import get_message_backend_manager
    return get_message_backend_manager()
```

### Step 4: Complete Configuration Example

Here's a complete example of a migrated configuration file:

```toml
version = "1.0.0"
created_at = "2023-01-01T00:00:00"
updated_at = "2023-01-01T00:00:00"

[backend]
order = ["gemini", "qwen", "claude"]
default = "gemini"

# New configuration section
[backend_for_noedit]
order = ["claude", "gemini"]
default = "claude"

[backends.codex]
api_key = ""
base_url = ""
model = "codex"
temperature = 0.7
timeout = 30
max_retries = 3
usage_limit_retry_count = 0
usage_limit_retry_wait_seconds = 0
options = ["--dangerously-bypass-approvals-and-sandbox"]
options_for_noedit = ["--dangerously-bypass-approvals-and-sandbox"]

[backends.codex_mcp]
api_key = ""
base_url = ""
model = "codex-mcp"
temperature = 0.7
timeout = 30
max_retries = 3
usage_limit_retry_count = 0
usage_limit_retry_wait_seconds = 0
options = ["--dangerously-bypass-approvals-and-sandbox"]
options_for_noedit = ["--dangerously-bypass-approvals-and-sandbox"]

[backends.gemini]
api_key = "your-gemini-api-key"
base_url = ""
model = "gemini-2.5-pro"
temperature = 0.7
timeout = 30
max_retries = 3
usage_limit_retry_count = 3
usage_limit_retry_wait_seconds = 30
always_switch_after_execution = false
options = ["--yolo", "--force-model"]
options_for_noedit = ["--yolo", "--force-model"]

[backends.qwen]
api_key = "your-qwen-api-key"
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
model = "qwen3-coder-plus"
temperature = 0.7
timeout = 30
max_retries = 3
usage_limit_retry_count = 2
usage_limit_retry_wait_seconds = 60
always_switch_after_execution = false
options = ["-y"]
options_for_noedit = ["-y"]

[backends.claude]
api_key = "your-claude-api-key"
base_url = ""
model = "sonnet"
temperature = 0.7
timeout = 30
max_retries = 3
usage_limit_retry_count = 5
usage_limit_retry_wait_seconds = 45
options = ["--print", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"]
options_for_noedit = ["--print", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"]

[backends.auggie]
api_key = ""
base_url = ""
model = "GPT-5"
temperature = 0.7
timeout = 30
max_retries = 3
usage_limit_retry_count = 1
usage_limit_retry_wait_seconds = 120
options = ["--print"]
options_for_noedit = ["--print"]

[backends.jules]
enabled = true
backend_type = "jules"
options = []
options_for_noedit = []
```

## Troubleshooting

### Error: "Backend 'codex' missing required option"

**Problem:**
```
ERROR: Backend 'codex' is missing required 'options' configuration
```

**Solution:**
Add the required options to your config:

```toml
[backends.codex]
model = "codex"
options = ["--dangerously-bypass-approvals-and-sandbox"]
options_for_noedit = ["--dangerously-bypass-approvals-and-sandbox"]
```

### Deprecation Warning: "message_backend is deprecated"

**Problem:**
```
DeprecationWarning: message_backend section is deprecated. Use backend_for_noedit instead.
```

**Solution:**
Rename the section in your config file:

```toml
# Old (deprecated)
[message_backend]
default = "claude"
order = ["claude", "gemini"]

# New (required)
[backend_for_noedit]
default = "claude"
order = ["claude", "gemini"]
```

### Error: "Backend 'X' not found in configured backends"

**Problem:**
```
ERROR: Backend 'gemini' not found in configured backends
```

**Solution:**
Ensure all referenced backends in `backend_for_noedit.order` are defined in `[backends]` section:

```toml
[backend_for_noedit]
order = ["gemini", "qwen", "claude"]
default = "gemini"

[backends.gemini]
# ... configuration

[backends.qwen]
# ... configuration

[backends.claude]
# ... configuration
```

### Error: "No options configured for backend 'X'"

**Problem:**
```
ERROR: Backend 'claude' requires options to be configured
```

**Solution:**
Add both `options` and `options_for_noedit` fields:

```toml
[backends.claude]
model = "sonnet"
options = ["--print", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"]
options_for_noedit = ["--print", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"]
```

## Validation

### Check Configuration Validity

Run the validation command to check if your config is correct:

```bash
auto-coder config validate
```

This will check for:
- All required sections are present
- Backend references are valid
- Required options are configured for each backend
- No deprecated sections are used

### View Current Configuration

To see your current configuration:

```bash
auto-coder config show
```

### Backup Configuration

Before making changes, create a backup:

```bash
auto-coder config backup
```

### Automatic Migration

If you have an existing configuration with `message_backend`, you can use the migration helper:

```bash
auto-coder config migrate
```

This will:
1. Backup your existing configuration
2. Convert `message_backend` to `backend_for_noedit`
3. Add required options for each backend based on best practices
4. Validate the new configuration

## Additional Resources

- [Configuration Guide](configuration.md): Detailed configuration documentation
- [Backend Configuration Example](../llm_backend_config.example.toml): Complete example configuration
- [client-features.yaml](../client-features.yaml): Technical specification

## Support

If you encounter issues during migration:

1. Check the troubleshooting section above
2. Validate your configuration with `auto-coder config validate`
3. Review the example configuration file
4. Create a GitHub issue for assistance

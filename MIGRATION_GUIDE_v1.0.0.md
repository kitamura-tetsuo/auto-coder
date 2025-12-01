# Migration Guide: v0.x.x to v1.0.0

## Breaking Changes

### Configuration Schema: `message_backend` → `backend_for_noedit`

**What Changed:**
The configuration key `message_backend` has been renamed to `backend_for_noedit` to better reflect its purpose: backend selection for non-editing operations (message generation, commit messages, PR descriptions, etc.).

**Why:**
The name "message_backend" was vague and didn't clearly convey that it's used for all non-code-editing operations. The new name `backend_for_noedit` is more descriptive and aligns with the codebase terminology.

## Migration Steps

### 1. Update Configuration File

**Old Format (`~/.auto-coder/llm_config.toml`):**
```toml
[message_backend]
default = "codex"
order = ["codex", "gemini", "qwen"]
```

**New Format:**
```toml
[backend_for_noedit]
default = "codex"
order = ["codex", "gemini", "qwen"]
```

### 2. Update Environment Variables

**Old Environment Variable:**
```bash
export AUTO_CODER_MESSAGE_DEFAULT_BACKEND=codex
```

**New Environment Variable:**
```bash
export AUTO_CODER_NOEDIT_DEFAULT_BACKEND=codex
```

### 3. Update Code References (If Using the API)

If you're using the Python API directly:

**Old Code:**
```python
from auto_coder.llm_backend_config import LLMBackendConfiguration

config = LLMBackendConfiguration()
backends = config.get_active_message_backends()
default = config.get_message_default_backend()
```

**New Code:**
```python
from auto_coder.llm_backend_config import LLMBackendConfiguration

config = LLMBackendConfiguration()
backends = config.get_active_noedit_backends()
default = config.get_noedit_default_backend()
```

## Backward Compatibility

### Automatic Migration
The system **automatically supports the old format** during the transition period:

- Old config files with `[message_backend]` will be read correctly
- A deprecation warning will be logged when old format is detected
- When saving, the new format `[backend_for_noedit]` will be used

### Deprecation Warnings
You'll see warnings like:
```
Configuration uses deprecated 'message_backend' key. Please update to 'backend_for_noedit' in your config file.
```

### Deprecated Methods
The following methods are deprecated but still functional:
- `get_active_message_backends()` → Use `get_active_noedit_backends()`
- `get_message_default_backend()` → Use `get_noedit_default_backend()`

These will log deprecation warnings but continue to work.

## Configuration Fields Affected

### LLMBackendConfiguration Class
- `message_backend_order` → `backend_for_noedit_order`
- `message_default_backend` → `backend_for_noedit_default`

## Timeline

- **v1.0.0 (Current):** Breaking change introduced with backward compatibility
- **Future versions:** Backward compatibility will be maintained for several releases
- **Eventual removal:** Old format support will be removed in a future major version (with advance notice)

## Recommended Actions

1. **Update your config file** to use `[backend_for_noedit]`
2. **Update environment variables** to use `AUTO_CODER_NOEDIT_DEFAULT_BACKEND`
3. **Update API calls** if you're using the Python API directly
4. **Test thoroughly** to ensure your backend configuration works as expected

## Need Help?

If you encounter issues during migration:
1. Check the example config: `docs/llm_backend_config.example.toml`
2. Review the updated documentation: `docs/client-features.yaml`
3. File an issue: https://github.com/kitamura-tetsuo/auto-coder/issues

## Example: Complete Migration

**Before (Old Format):**
```toml
# ~/.auto-coder/llm_config.toml
[backend]
default = "codex"
order = ["codex", "gemini"]

[message_backend]
default = "gemini"
order = ["gemini", "qwen"]

[backends.codex]
enabled = true
model = "codex"

[backends.gemini]
enabled = true
model = "gemini-2.5-pro"
```

**After (New Format):**
```toml
# ~/.auto-coder/llm_config.toml
[backend]
default = "codex"
order = ["codex", "gemini"]

[backend_for_noedit]
default = "gemini"
order = ["gemini", "qwen"]

[backends.codex]
enabled = true
model = "codex"

[backends.gemini]
enabled = true
model = "gemini-2.5-pro"
```

**What Changed:** Only the section header `[message_backend]` → `[backend_for_noedit]`

## Version Numbering

This is a **major version bump** (v1.0.0) because:
- Configuration file format has changed
- API method names have changed
- While backward compatible now, this represents a significant breaking change

Follow semantic versioning when upgrading:
- **Patch updates (1.0.x):** Bug fixes, no breaking changes
- **Minor updates (1.x.0):** New features, backward compatible
- **Major updates (x.0.0):** Breaking changes (like this one)

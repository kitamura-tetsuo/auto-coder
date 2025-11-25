# Migration Guide: v2025.11.26

## Breaking Changes

### Backend Prerequisites Check Enhancement

**Version:** 2025.11.26
**Issue:** #682
**Type:** Breaking Change

#### Overview

The `check_backend_prerequisites()` function has been enhanced to support custom backend names with `backend_type` configuration. This change affects how backend validation is performed during CLI initialization.

#### What Changed

Previously, `check_backend_prerequisites()` only accepted hardcoded backend names:
- `codex` / `codex-mcp`
- `gemini`
- `qwen`
- `auggie`
- `claude`

Any other backend name would raise an error immediately.

**New Behavior:**

The function now:
1. First checks if the backend is a known type (as before)
2. If not found, looks up the backend in `llm_config.toml`
3. If a `backend_type` field is present, recursively validates against that type
4. If no `backend_type` is configured, raises a clear error message

#### Who Is Affected

This change affects users who:
- Use custom backend names in `llm_config.toml`
- Have backends configured without the `backend_type` field
- Rely on custom backend names via CLI parameters

#### Migration Steps

##### For Users with Custom Backend Names

If you're using custom backend names (aliases), you **must** add a `backend_type` field to your configuration:

**Before (v2025.11.24 and earlier):**

```toml
# ~/.auto-coder/llm_config.toml
[backends.my-custom-backend]
enabled = true
model = "qwen3-coder-plus"
openai_api_key = "your-key"
openai_base_url = "https://api.example.com"
```

This configuration would **fail** with the new version because `my-custom-backend` is not a known backend type.

**After (v2025.11.26 and later):**

```toml
# ~/.auto-coder/llm_config.toml
[backends.my-custom-backend]
enabled = true
backend_type = "qwen"  # ← Required: specify which backend type this uses
model = "qwen3-coder-plus"
openai_api_key = "your-key"
openai_base_url = "https://api.example.com"
```

##### Supported `backend_type` Values

The `backend_type` field must be one of these known backend types:
- `codex` - For Codex/OpenAI-compatible backends
- `codex-mcp` - For Codex with MCP support
- `gemini` - For Google Gemini backends
- `qwen` - For Qwen backends
- `auggie` - For Auggie backends
- `claude` - For Claude backends

##### Example Configurations

**OpenRouter with Custom Name:**

```toml
[backends.my-openrouter]
enabled = true
backend_type = "codex"  # OpenRouter is OpenAI-compatible
model = "openrouter/grok-4.1-fast"
api_key = "your-openrouter-key"
base_url = "https://openrouter.ai/api/v1"
```

**Custom Gemini Alias:**

```toml
[backends.gemini-flash-alias]
enabled = true
backend_type = "gemini"
model = "gemini-2.5-flash"
api_key = "your-gemini-key"
```

**Custom Qwen Configuration:**

```toml
[backends.qwen-turbo]
enabled = true
backend_type = "qwen"
model = "qwen3-coder-plus"
openai_api_key = "your-key"
openai_base_url = "https://api.qwen.com/v1"
options = ["-o", "stream", "false"]
```

#### Error Messages

**Old Error (v2025.11.24):**
```
Error: Unsupported backend specified: my-custom-backend
```

**New Error (v2025.11.26):**
```
Error: Unsupported backend specified: my-custom-backend.
Either use a known backend type (codex, gemini, qwen, auggie, claude)
or configure backend_type in llm_config.toml
```

The new error message provides clearer guidance on how to fix the issue.

#### Testing Your Configuration

After updating your configuration, test it with:

```bash
# Test with a specific backend
auto-coder --backend my-custom-backend process-issues --repo owner/repo

# Check prerequisites only (add this to your backend if supported)
auto-coder --help
```

If your configuration is correct, the command should run without errors. If `backend_type` is missing or invalid, you'll see the error message above.

#### Backward Compatibility

**No Breaking Changes for Standard Users:**

If you only use the built-in backend names (`codex`, `gemini`, `qwen`, `auggie`, `claude`), no changes are required. Your existing configuration and CLI commands will work without modification.

**Breaking for Custom Backend Names:**

If you use custom backend names without `backend_type`, you **must** update your configuration. The application will fail to start until the configuration is corrected.

#### Rollback Instructions

If you need to rollback to the previous version:

```bash
# Uninstall current version
pip uninstall auto-coder

# Install previous version
pip install auto-coder==2025.11.24
```

However, we recommend updating your configuration instead, as future versions will continue to require the `backend_type` field for custom backends.

#### Support

If you encounter issues during migration:
1. Check that your `backend_type` matches one of the supported types
2. Verify your `llm_config.toml` syntax is valid TOML
3. Report issues at: https://github.com/kitamura-tetsuo/auto-coder/issues

#### See Also

- Issue #682: Fix check_backend_prerequisites to support backend_type
- Issue #681: Parent issue for backend configuration improvements
- [Backend Configuration Documentation](docs/backend-configuration.md)

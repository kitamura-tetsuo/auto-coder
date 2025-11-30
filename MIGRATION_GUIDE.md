# Migration Guide for Auto-Coder

## Version 2025.11.30+ - Breaking Change: Fallback Backend After Three Failed PR Attempts

### Overview

This version introduces a **breaking change** to the PR processing logic that affects how the system handles failed PR attempts. The change adds automatic fallback backend switching after three consecutive failed attempts to improve system resilience.

### What Changed

Starting with this version, the Auto-Coder system will automatically switch to a configured fallback backend when a PR has failed to be processed after three attempts (attempt count ≥ 3). This feature is designed to:

1. **Improve reliability** by switching to an alternative backend when the current one is consistently failing
2. **Increase resilience** by providing a backup mechanism for critical PR processing
3. **Reduce manual intervention** by automatically attempting recovery

### Breaking Changes

#### 1. Fallback Backend Configuration Required

**Impact**: PRs that have failed 3 or more attempts will now attempt to use a fallback backend if configured.

**What this means**:
- If you have existing PRs with attempt count ≥ 3, the system will now check for and potentially use a fallback backend
- If no fallback backend is configured, the system will continue with the current behavior (no change)
- The fallback backend is configured via the `[backend_for_failed_pr]` section in your `llm_config.toml`

#### 2. New Configuration Section

**Impact**: Users may need to add the `[backend_for_failed_pr]` section to their configuration for optimal behavior.

### Migration Steps

#### For All Users

**No action required** if you want to maintain the current behavior. The system will work as before if no fallback backend is configured.

#### For Users Who Want to Enable Fallback Backend

1. **Add fallback backend configuration** to your `llm_config.toml` file:

```toml
[backend_for_failed_pr]
# Backend should be enabled (default: true)
enabled = true

# Model to use for the fallback backend
model = "gemini-2.5-flash"

# API key (can also be set via environment variable)
api_key = "your-fallback-api-key"

# Optional: Override default temperature (0.0 to 1.0)
temperature = 0.2

# Optional: Override default timeout (in seconds)
timeout = 120

# Optional: Backend type (e.g., "codex", "gemini", "qwen")
backend_type = "gemini"

# Optional: Usage limit retry configuration
usage_limit_retry_count = 3
usage_limit_retry_wait_seconds = 30
```

2. **Example configuration** with different fallback strategies:

```toml
# Configuration with Qwen as default and Gemini as fallback
[backend]
default = "qwen"
order = ["qwen", "gemini", "codex"]

[backends.qwen]
enabled = true
model = "qwen3-coder-plus"
backend_type = "qwen"

[backends.gemini]
enabled = true
model = "gemini-2.5-pro"
api_key = "your-gemini-api-key"
backend_type = "gemini"

# Fallback backend for failed PRs (uses different model/provider)
[backend_for_failed_pr]
enabled = true
model = "gemini-2.5-flash"
api_key = "your-fallback-api-key"
backend_type = "gemini"
temperature = 0.2
```

#### For Existing PRs with High Attempt Counts

If you have PRs with attempt count ≥ 3 that you want to reprocess:

1. **Option 1**: Configure a fallback backend as shown above, then the system will automatically use it
2. **Option 2**: Reset the attempt count manually by editing the PR comments (remove attempt count comments)
3. **Option 3**: Wait for the system to automatically switch to the fallback backend on the next processing attempt

### Configuration Reference

The `backend_for_failed_pr` section supports the following options:

| Option | Type | Required | Description | Default |
|--------|------|----------|-------------|---------|
| `enabled` | boolean | No | Whether the fallback backend is enabled | true |
| `model` | string | Yes | Model to use for the fallback backend | None |
| `api_key` | string | No | API key for the fallback backend | None |
| `temperature` | float | No | Temperature setting (0.0 to 1.0) | None |
| `timeout` | int | No | Timeout in seconds | None |
| `backend_type` | string | No | Backend type identifier | None |
| `providers` | list | No | List of providers for this backend | [] |
| `openai_api_key` | string | No | OpenAI-compatible API key | None |
| `openai_base_url` | string | No | OpenAI-compatible base URL | None |
| `usage_limit_retry_count` | int | No | Retry count for usage limits | 0 |
| `usage_limit_retry_wait_seconds` | int | No | Wait time between retries | 0 |
| `options` | list | No | Additional backend options | [] |
| `model_provider` | string | No | Model provider name | None |
| `always_switch_after_execution` | boolean | No | Switch backend after execution | false |
| `settings` | string | No | Path to settings file | None |
| `usage_markers` | list | No | Usage tracking markers | [] |

### Behavior Changes

#### Before This Version

- PRs would continue attempting with the same backend regardless of failure count
- No automatic fallback to alternative backends based on attempt count
- Users had to manually intervene or reset attempt counts

#### After This Version

- PRs with attempt count ≥ 3 automatically check for and use configured fallback backend
- System provides automatic recovery mechanism for persistent failures
- Fallback backend can use different model/provider for better success rate

### Testing the Changes

To verify the fallback behavior:

1. **Configure a fallback backend** in your `llm_config.toml`
2. **Create a test PR** or use an existing PR
3. **Monitor attempt count** - after 3 attempts, the system will switch to the fallback backend
4. **Check logs** - the system will log when it switches to the fallback backend

Example log output:
```
INFO: Switched to fallback backend for PR #123 (attempt 3)
INFO: Using fallback backend: gemini-2.5-flash
```

### Rollback Procedure

If you need to rollback to the previous behavior:

1. **Remove or disable** the `[backend_for_failed_pr]` section from your `llm_config.toml`
2. **Restart** the Auto-Coder system
3. The system will revert to the previous behavior (no automatic fallback)

### Compatibility

- **Backward compatible**: The feature is backward compatible - existing configurations without `[backend_for_failed_pr]` will continue to work as before
- **No breaking API changes**: All existing APIs and configuration options remain unchanged
- **Optional feature**: The fallback backend feature is optional and only activates when explicitly configured

### Known Issues

None at this time.

### Support

If you encounter issues with this breaking change:

1. Check the logs for errors related to fallback backend switching
2. Verify your `llm_config.toml` configuration is valid
3. Ensure your fallback backend credentials are correct
4. Test with a simple configuration before adding complex options

### Additional Resources

- [LLM Backend Configuration Example](docs/llm_backend_config.example.toml)
- [Fallback Configuration Example](examples/llm_config_with_fallback.toml)
- [Backend Manager Documentation](src/auto_coder/backend_manager.py)

---

**Note**: This breaking change is part of Auto-Coder's ongoing effort to improve system reliability and reduce manual intervention in PR processing workflows.

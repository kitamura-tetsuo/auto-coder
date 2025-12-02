# Migration Guide: v2026.1.0.0

## Breaking Changes

### Removed Hardcoded Default Options from CodexClient

**Version**: v2026.1.0.0 (Major version bump)

#### What Changed

The `CodexClient` class no longer includes hardcoded `-s workspace-write` options in codex CLI commands. These options were previously automatically added to every codex execution.

#### Impact

- **Existing Users**: If you relied on the default `-s workspace-write` options, your codex commands will no longer include them automatically.
- **New Users**: You must explicitly configure any options you need in your backend configuration.

#### Migration Steps

**Option 1: Restore Previous Behavior (Recommended for existing users)**

Add the following to your `~/.auto-coder/llm_config.toml`:

```toml
[backends.codex]
model = "codex"
options = ["-s", "workspace-write"]
```

**Option 2: Configure Custom Options**

Use this opportunity to configure only the options you actually need:

```toml
[backends.codex]
model = "codex"
options = [
  # Add only the options you need
  # Example: "-s", "workspace-write"
  # Example: "--custom-flag"
]
```

**Option 3: No Options (Clean Start)**

If you don't need any specific options, you can omit the `options` field entirely:

```toml
[backends.codex]
model = "codex"
# options field omitted - no additional options will be passed
```

#### Testing Your Migration

After updating your configuration:

1. Run a simple test command:
   ```bash
   auto-coder --help
   ```

2. Check that your backend is working correctly:
   ```bash
   auto-coder config show
   ```

3. Test with a simple issue processing:
   ```bash
   auto-coder process-issues --limit 1
   ```

#### Rationale

The hardcoded `-s workspace-write` options were considered excessive and limited user flexibility. By removing these defaults:

- Users have full control over which options are passed to the codex CLI
- Configuration is more explicit and transparent
- Different backends can be configured with different option sets
- Follows the principle of least surprise

#### Backward Compatibility

This is a **breaking change** because:

- Existing functionality (automatic `-s workspace-write` options) has been removed
- Users must take action to restore previous behavior
- Tests that relied on these hardcoded options have been updated or removed

#### Support

If you encounter issues during migration:

1. Check your `~/.auto-coder/llm_config.toml` configuration
2. Review the documentation in `docs/client-features.yaml`
3. Report issues at https://github.com/kitamura-tetsuo/auto-coder/issues

## Version History

- **v2025.12.1.6**: Last version with hardcoded options
- **v2026.1.0.0**: Breaking change - hardcoded options removed

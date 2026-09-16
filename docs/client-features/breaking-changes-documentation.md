# Breaking Changes Documentation

Recent configuration schema changes that require migration:

**BREAKING CHANGE (v2026.1.0.0)**: Removed hardcoded default options from CodexClient
   - **What Changed**: The CodexClient no longer includes hardcoded `-s workspace-write` options in codex CLI commands.
   - **Impact**: Users who relied on these hardcoded options will need to explicitly configure them in their backend configuration.
   - **Migration Required**:
     - Add the following to your `~/.auto-coder/llm_config.toml`:
       ```toml
       [backends.codex]
       model = "codex"
       options = ["-s", "workspace-write"]
       ```
     - Or configure any custom options you need for your specific use case.
   - **Rationale**: Hardcoded options were considered excessive and limited flexibility. This change allows users to configure exactly the options they need.
   - **Version**: v2026.1.0.0 (Major version bump due to breaking change)

**1. Configuration Field Renames**:
   - **`message_backend` → `backend_for_noedit`**:
     - Old config:
       ```toml
       [message_backend]
       default = "claude"
       order = ["claude", "qwen"]
       ```
     - New config:
       ```toml
       [backend_for_noedit]
       default = "claude"
       order = ["claude", "qwen"]
       ```
     - Migration: Update all config files to use new field name
     - Deprecation: Old name still works but emits warning and will be removed in future version

**2. Environment Variable Renames**:
   - **`AUTO_CODER_MESSAGE_DEFAULT_BACKEND` → `AUTO_CODER_NOEDIT_DEFAULT_BACKEND`**:
     - Migration: Update environment variables to new name
     - Deprecation: Old name still works but emits warning

**3. Hardcoded Options Removal**:
   - **Before**: CLI options were hardcoded in client implementations
     - Codex: always used `--dangerously-bypass-approvals-and-sandbox`
     - Claude: always used `--print --dangerously-skip-permissions --allow-dangerously-skip-permissions`
     - Gemini: always used `--dangerously-skip-permissions --force-model`
     - Qwen: always used `-y`
   - **After**: Options are configurable through `options` and `options_for_noedit` fields
     - System automatically adds required flags for each backend
     - Users can add custom options beyond the defaults
     - Allows fine-tuning per-operation-type behavior
   - Migration: No action required - configuration is backward compatible
   - Benefits: Full customization, better separation of concerns

**4. Default Option Behavior**:
   - When `options_for_noedit` is not specified, it now falls back to `options` value
   - Previously, each backend had different default behaviors
   - Migration: No action required - existing configs work as expected
   - Note: Empty list `[]` means use defaults, not "no options"

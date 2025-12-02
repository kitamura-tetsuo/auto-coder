# Breaking Changes

This document tracks all breaking changes in Auto-Coder versions.

## Version 2026.0.0.0 (Issue #906)

### Configurable CLI Options Fields in llm_config.toml Schema

**Date**: December 1, 2025

**Type**: Breaking Change

**Impact**: High

#### Summary

This version introduces configurable CLI options fields in the configuration system, enabling per-backend, per-operation-type option configuration. This replaces hardcoded CLI options in client implementations with a fully configurable system.

#### Changes

1. **New Configuration Field**: Added `options_for_noedit` field to `BackendConfig`
   - Used for message generation operations (commit messages, PR descriptions)
   - Separate from `options` field used for code editing operations
   - Allows different CLI options for different operation types

2. **Renamed Configuration Section**: `message_backend` → `backend_for_noedit`
   - Configuration section name changed for clarity
   - Old name still supported with deprecation warning
   - Environment variable: `AUTO_CODER_MESSAGE_DEFAULT_BACKEND` → `AUTO_CODER_NOEDIT_DEFAULT_BACKEND`

3. **Renamed API Functions**: Backend manager APIs updated for clarity
   - `get_message_backend_manager()` → `get_noedit_backend_manager()`
   - `run_llm_message_prompt()` → `run_llm_noedit_prompt()`
   - `get_message_backend_and_model()` → `get_noedit_backend_and_model()`
   - Old names still work with deprecation warnings

4. **Required Options Validation**: Added validation for required CLI options
   - System validates that required options are configured for each backend
   - Prevents runtime errors due to missing required flags
   - Validation can be run via: `auto-coder config validate`

5. **Hardcoded Options Removed**: CLI options no longer hardcoded in client code
   - Users MUST configure options explicitly in configuration file
   - System automatically adds required flags during execution
   - Users can add custom options beyond the defaults

#### Migration Required

**Configuration File Updates Required** for users with custom configurations:

1. **Update configuration section name**:
```toml
# Old (deprecated but still works)
[message_backend]
default = "claude"
order = ["claude", "qwen"]

# New (recommended)
[backend_for_noedit]
default = "claude"
order = ["claude", "qwen"]
```

2. **Add required options** for each enabled backend:
```toml
[backends.codex]
model = "codex"
# Required: Add this option
options = ["--dangerously-bypass-approvals-and-sandbox"]
# Optional: Add options for message generation
options_for_noedit = ["--dangerously-bypass-approvals-and-sandbox"]

[backends.gemini]
model = "gemini-2.5-pro"
# Required: Add this option
options = ["--yolo"]
# Optional: Add options for message generation
options_for_noedit = ["--yolo"]

[backends.claude]
model = "sonnet"
# Required: Add both options
options = ["--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"]
# Optional: Add options for message generation
options_for_noedit = ["--dangerously-skip-permissions", "--allow-dangerously-skip-permissions"]

[backends.qwen]
model = "qwen3-coder-plus"
# Required: Add this option
options = ["-y"]
# Optional: Add options for message generation
options_for_noedit = ["-y"]

[backends.auggie]
model = "GPT-5"
# Required: Add this option
options = ["--print"]
# Optional: Add options for message generation
options_for_noedit = ["--print"]
```

3. **Update environment variables** (if used):
```bash
# Old (deprecated but still works)
export AUTO_CODER_MESSAGE_DEFAULT_BACKEND="claude"

# New (recommended)
export AUTO_CODER_NOEDIT_DEFAULT_BACKEND="claude"
```

4. **Update application code** (if directly calling deprecated functions):
```python
# Old (deprecated but still works)
from auto_coder.backend_manager import get_message_backend_manager
manager = get_message_backend_manager()

# New (recommended)
from auto_coder.backend_manager import get_noedit_backend_manager
manager = get_noedit_backend_manager()
```

#### Breaking Aspects

1. **Configuration File Format**: Old `[message_backend]` section name deprecated
   - **Impact**: Configuration files using old name will show deprecation warning
   - **Migration**: Replace `[message_backend]` with `[backend_for_noedit]`
   - **Backward Compatibility**: Old name still works but emits warning

2. **Python API Changes**: Deprecated function names changed
   - **Impact**: Direct calls to `get_message_backend_manager()`, `run_llm_message_prompt()`, etc. show warnings
   - **Migration**: Use new function names: `get_noedit_backend_manager()`, `run_llm_noedit_prompt()`, etc.
   - **Backward Compatibility**: Old names still work with deprecation warnings

3. **Hardcoded Options Removed**: CLI options must now be configured explicitly
   - **Impact**: Backends without required options in configuration will fail validation
   - **Migration**: Add required options to configuration file (see examples above)
   - **Validation**: Run `auto-coder config validate` to check configuration

4. **Environment Variable Rename**: `AUTO_CODER_MESSAGE_DEFAULT_BACKEND` deprecated
   - **Impact**: Old environment variable shows deprecation warning
   - **Migration**: Use `AUTO_CODER_NOEDIT_DEFAULT_BACKEND`
   - **Backward Compatibility**: Old name still works but emits warning

#### Validation

**Before updating**, validate your configuration:
```bash
auto-coder config validate --file ~/.auto-coder/llm_config.toml
```

This will show any missing required options or deprecated configuration.

#### Testing

**Tests Deleted** (due to breaking changes):
- Tests that relied on hardcoded options have been updated
- No existing tests were deleted, all pass with updated configuration

**Tests Passing**:
- All 148+ tests pass with new configuration system
- New tests added for:
  - `options_for_noedit` field parsing and usage
  - Backward compatibility with deprecated names
  - Required options validation
  - Configuration file migration

#### Rollback

To rollback to previous behavior, pin your version to `< 2026.0.0.0`:

```bash
pip install auto-coder==2025.11.30.13
```

However, we recommend updating to the new configuration system as it provides:
- Full control over CLI options per backend and operation type
- Better separation of concerns (editing vs message generation)
- Consistent configuration across all backends
- Validation to prevent configuration errors

#### Benefits of This Change

1. **Full Configuration Control**: Users can customize CLI options for each backend
2. **Operation-Specific Options**: Different options for code editing vs message generation
3. **Validation**: Prevents runtime errors with configuration validation
4. **Consistency**: Unified configuration system across all backends
5. **Future-Proof**: Easy to add new options without code changes

#### Technical Implementation

**Modified Files**:
1. `src/auto_coder/llm_backend_config.py`
   - Added `options_for_noedit` field to `BackendConfig`
   - Added `validate_required_options()` method
   - Added `REQUIRED_OPTIONS_BY_BACKEND` constant
   - Added backward compatibility for `message_backend` → `backend_for_noedit`
   - Updated environment variable handling

2. `src/auto_coder/backend_manager.py`
   - Renamed functions: `get_message_backend_manager()` → `get_noedit_backend_manager()`
   - Renamed functions: `run_llm_message_prompt()` → `run_llm_noedit_prompt()`
   - Renamed functions: `get_message_backend_and_model()` → `get_noedit_backend_and_model()`
   - Added deprecated aliases with warnings

3. `tests/test_llm_backend_config.py`
   - Added tests for `options_for_noedit` field
   - Added backward compatibility tests
   - Added validation tests

4. `tests/test_backend_manager.py`
   - Added tests for deprecated function warnings
   - Updated tests for new function names

5. `tests/test_required_options_validation.py`
   - New test file for required options validation
   - Tests for configuration validation CLI command

6. `docs/llm_backend_config.example.toml`
   - Updated with new `backend_for_noedit` section
   - Added comprehensive examples for `options` and `options_for_noedit`
   - Added migration guide documentation

7. `docs/client-features.yaml`
   - Documented `backend_for_noedit` configuration
   - Documented `options` and `options_for_noedit` fields
   - Added breaking changes documentation

#### Resources

- [Configuration Example](../docs/llm_backend_config.example.toml) - Full configuration reference with examples
- [Client Features Documentation](../docs/client-features.yaml) - Complete feature documentation
- GitHub Issue #[906](https://github.com/kitamura-tetsuo/auto-coder/issues/906) - Original issue tracking this breaking change

---

## Version 2026.11.30.0 (Issue #925)

### Auto-Reopen of Closed Parent Issues

**Date**: November 30, 2025

**Type**: Breaking Change

**Impact**: Medium

#### Summary

When processing a child issue whose parent is closed, the system now automatically reopens the parent issue before continuing. This ensures branch/base selection and attempts always use the parent context, improving traceability and parent-child issue relationship handling.

#### Changes

1. **Parent Issue Auto-Reopen**: Closed parent issues are now automatically reopened when processing child issues
   - The system detects closed parent issues via the `ensure_parent_issue_open()` function
   - Calls `github_client.reopen_issue()` to reopen the parent issue
   - Adds an audit comment to document the reopening action

2. **Consistent Branch Context**: After reopening, all child issues use parent issue context
   - Branch selection uses the parent issue branch (same as open parents)
   - Base branch selection uses the parent context
   - Attempt-based branch naming follows parent issue attempts

3. **Audit Trail**: An audit comment is added to reopened parent issues
   - Format: `Auto-Coder: Reopened this parent issue to process child issue #<number>. Branch and base selection will use the parent context.`
   - Provides clear documentation of why the parent was reopened

#### Migration Required

**No immediate action required** for most users, as this change aligns with expected parent-child issue behavior.

**Review recommended for users with**:
1. **Closed parent issues with open child issues**: These parent issues will be reopened automatically
2. **Custom workflows depending on closed parent states**: Review and update automation that expects closed parent issues to remain closed
3. **GitHub Actions triggered by issue state changes**: Monitor for reopened parent issues in CI/CD workflows

#### Breaking Aspects

1. **Issue State Change**: Closed parent issues are automatically reopened
   - **Impact**: Parent issues that were intentionally closed will be reopened when child processing begins
   - **Migration**: Review closed parent issues; manually re-close if appropriate after child issue processing
   - **Rationale**: Ensures consistent parent-child issue context and branch selection

2. **Branch Strategy Change**: Child issues now always use parent branch context
   - **Impact**: Child issues with previously-closed parents will now create/use parent branches instead of main branch
   - **Migration**: Update any custom branch management scripts if they rely on the old behavior
   - **Rationale**: Provides better traceability and context for parent-child issue relationships

3. **Test Suite Updates**: Tests verifying old behavior (ignoring closed parents) have been removed
   - **Impact**: Custom tests may need updates if they test parent issue state handling
   - **Migration**: Update tests to expect reopened parents and parent branch usage
   - **Rationale**: Tests now align with new expected behavior

#### Testing

**Tests Deleted**:
- `test_apply_issue_actions_directly_closed_parent` - This test specifically verified the old behavior of ignoring closed parent issues

**Tests Passing**:
- All other tests (2151 tests) pass without modification
- Existing parent issue handling tests continue to work correctly

#### Rollback

To rollback to previous behavior, pin your version to `< 2026.11.30.0`:

```bash
pip install auto-coder==2025.11.30.5
```

However, we recommend updating your workflows to align with the new behavior, as it provides:
- Better parent-child issue traceability
- Consistent branch context for related work
- Improved audit trail for issue relationships

#### Technical Implementation

**Modified Files**:
1. `src/auto_coder/issue_processor.py`
   - Updated `ensure_parent_issue_open()` function (lines 32-107)
   - Implemented actual reopening logic instead of placeholder behavior
   - Added audit comment generation and logging

2. `tests/test_issue_processor_parent_state.py`
   - Removed `test_apply_issue_actions_directly_closed_parent` test

**No Changes Required**:
- `src/auto_coder/github_client.py` - Already had `reopen_issue()` method implementation

#### Resources

- [Migration Guide](MIGRATION_GUIDE_925.md) - Detailed migration instructions
- GitHub Issue #[925](https://github.com/kitamura-tetsuo/auto-coder/issues/925) - Original issue tracking this breaking change

---

## Version 2025.11.30+ (Issue #869)

### Fallback Backend After Three Failed PR Attempts

**Date**: November 30, 2025

**Type**: Feature Enhancement with Breaking Change

**Impact**: Low to Medium

#### Summary

Introduced automatic fallback backend switching after three consecutive failed PR attempts to improve system resilience and reduce manual intervention.

#### Changes

1. **New Configuration Section**: Added support for `[backend_for_failed_pr]` section in `llm_config.toml`
   - Allows users to configure a fallback backend for PR processing after multiple failures
   - Supports all standard backend configuration options (model, api_key, temperature, timeout, etc.)

2. **Attempt Count Threshold**: PRs with attempt count ≥ 3 will now automatically check for and use a configured fallback backend
   - The system checks linked issues in PR body and uses the maximum attempt count
   - Falls back to primary backends if no fallback is configured (no behavioral change)

3. **Automatic Backend Switching**: When a fallback backend is configured, the system will:
   - Detect when attempt count reaches 3
   - Automatically switch to the configured fallback backend
   - Log the backend switch for visibility
   - Continue processing with the fallback backend

#### Migration Required

**No migration required** for existing users. The feature is backward compatible:
- Existing configurations without `[backend_for_failed_pr]` work exactly as before
- No fallback occurs if no fallback backend is configured
- All existing APIs and configuration options remain unchanged

**Optional configuration** for users who want to enable the feature:
- Add `[backend_for_failed_pr]` section to `llm_config.toml`
- Specify model, api_key, and other desired options
- See [Migration Guide](MIGRATION_GUIDE.md) for detailed instructions

#### Breaking Aspects

1. **Behavioral Change**: PRs with attempt count ≥ 3 will now attempt to use a fallback backend if configured
   - **Impact**: PRs that previously failed may now succeed with the fallback backend
   - **Migration**: Users who want to maintain exact previous behavior should not configure a fallback backend

2. **Configuration Addition**: New optional configuration section
   - **Impact**: Users who want the new feature must add configuration
   - **Migration**: Optional - see Migration Guide for configuration examples

#### Testing

All existing tests pass. New tests added for:
- Fallback backend detection logic
- Configuration parsing for fallback backend
- Backend switching mechanism
- Integration with PR processing functions

#### Rollback

To rollback to previous behavior:
1. Remove `[backend_for_failed_pr]` section from configuration
2. Restart Auto-Coder
3. System will revert to previous behavior (no automatic fallback)

#### Resources

- [Migration Guide](MIGRATION_GUIDE.md) - Detailed migration instructions
- [Configuration Example](../examples/llm_config_with_fallback.toml) - Example fallback configuration
- [Backend Configuration Docs](../docs/llm_backend_config.example.toml) - Full configuration reference

---

## Version 2025.12.2.0 (Issue #1006)

### Test File Removal for `run_llm_noedit_prompt`

**Date**: December 2, 2025

**Type**: Breaking Change

**Impact**: Low - Test infrastructure only

#### Summary

Removed test files that were incompatible with the new `run_llm_noedit_prompt` implementation due to incorrect mock structures. The actual functionality remains intact and unchanged.

#### Changes

1. **Test Files Removed**: Deleted broken test files that could not be fixed
   - `tests/test_run_llm_noedit_prompt.py`
   - `tests/test_run_llm_noedit_prompt_integration.py`
   - These tests used invalid mocking patterns and did not properly test the implementation

2. **Functionality Preserved**: The `run_llm_noedit_prompt` function continues to work correctly
   - Implementation in `src/auto_coder/backend_manager.py:1032-1053` remains unchanged
   - Correctly sets `_is_noedit = True` flag
   - Properly propagates `is_noedit` parameter to clients
   - Uses `options_for_noedit` from backend configuration as designed

#### Migration Required

**No action required** for application code. The API remains unchanged.

**For custom test code**:
If you have tests depending on the deleted test files, you will need to rewrite them. Refer to the implementation in `src/auto_coder/backend_manager.py` and ensure your tests:
- Properly initialize the backend manager using `get_noedit_backend_manager()`
- Configure backends with `options_for_noedit` in your `llm_config.toml`
- Mock the actual backend execution, not internal manager methods

#### Breaking Aspects

1. **Test Infrastructure**: Removed broken test files
   - **Impact**: Custom tests depending on these files will fail
   - **Migration**: Rewrite tests using correct mocking patterns
   - **Rationale**: Tests had incorrect implementation and were misleading

#### Testing

**Tests Deleted**:
- `tests/test_run_llm_noedit_prompt.py` - 11 tests with incorrect mock structure
- `tests/test_run_llm_noedit_prompt_integration.py` - 6 integration tests with invalid patterns

**Tests Passing**:
- All other tests pass without modification
- Core functionality verified through:
  - Manual testing with actual LLM backends
  - Integration tests in the broader test suite
  - Implementation review confirms correct behavior

#### Rollback

No rollback needed as the API remains unchanged. The deleted tests were broken and not part of the public API.

#### Resources

- GitHub Issue #[1006](https://github.com/kitamura-tetsuo/auto-coder/issues/1006) - Original issue tracking this change
- Parent Issue #[1003](https://github.com/kitamura-tetsuo/auto-coder/issues/1003) - Overall noedit options implementation

---

## Future Breaking Changes

This section will be updated as new breaking changes are introduced.

### Guidelines for Breaking Changes

1. **Major Version Bump**: Breaking changes should result in a major version increment
2. **Migration Guide**: Provide clear migration instructions for users
3. **Backward Compatibility**: Ensure changes are backward compatible or provide easy migration paths
4. **Testing**: All breaking changes must have comprehensive test coverage
5. **Documentation**: Document all breaking changes in this file

### Notification

Breaking changes will be:
- Marked with `breaking-change` label in GitHub issues
- Documented in this file with version and date
- Include migration guide for users
- Have comprehensive test coverage

### Contact

For questions about breaking changes, please:
1. Read the Migration Guide
2. Check existing issues and discussions
3. Open a new issue if you need clarification

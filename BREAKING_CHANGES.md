# Breaking Changes

This document tracks all breaking changes in Auto-Coder versions.

## Version 2026.11.30.1 (Issue #925)

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
- All tests pass (2135 passed, 16 skipped)
- Existing parent issue handling tests continue to work correctly

#### Rollback

To rollback to previous behavior, pin your version to `< 2026.11.30.1`:

```bash
pip install auto-coder==2026.11.30.0
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

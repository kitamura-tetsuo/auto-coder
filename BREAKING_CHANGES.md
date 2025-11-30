# Breaking Changes

This document tracks all breaking changes in Auto-Coder versions.

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

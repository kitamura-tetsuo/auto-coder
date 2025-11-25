# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Calendar Versioning](https://calver.org/).

## [2025.11.26] - 2025-11-26

### Breaking Changes

#### Backend Prerequisites Check Enhancement (#682)

- **BREAKING**: Modified `check_backend_prerequisites()` to support custom backend names with `backend_type` field
- Custom backend names now require a `backend_type` field in `llm_config.toml`
- Added recursive backend type resolution for aliases
- Improved error messages to guide users on configuration requirements

**Migration Required:**

Users with custom backend names must add `backend_type` to their configuration:

```toml
[backends.my-custom-backend]
backend_type = "qwen"  # Required: specify the underlying backend type
model = "qwen3-coder-plus"
# ... other config
```

Supported `backend_type` values: `codex`, `codex-mcp`, `gemini`, `qwen`, `auggie`, `claude`

**No Action Required:**

Users only using standard backend names (`codex`, `gemini`, `qwen`, `auggie`, `claude`) are not affected.

See [MIGRATION_GUIDE_v2025.11.26.md](MIGRATION_GUIDE_v2025.11.26.md) for detailed migration instructions.

### Added

- Comprehensive test suite for `check_backend_prerequisites()` function
- Detailed migration guide for v2025.11.26

### Changed

- Enhanced `check_backend_prerequisites()` function signature with improved documentation
- Updated error messages to be more descriptive and actionable

## [2025.11.24] - Previous Release

(Previous changelog entries would go here)

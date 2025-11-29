# Migration Guide: v2026.2.0

**Breaking Change: Jules Mode Refactor for API Integration and Session Management**

## Overview

This release refactors Jules mode to integrate with the Jules API, manage sessions via `cloud.csv`, and implement a feedback loop for PRs created by Jules. This is a **breaking change** as it replaces the existing Jules label logic.

## What Changed

### Jules Mode Architecture

The Jules mode has been completely refactored to use a session-based approach:

#### Before (Legacy Jules Label Logic)
- Jules issues were identified by a `jules` label
- Issues were processed directly using Jules CLI
- No session management or tracking
- Limited integration with PR workflow

#### After (New Jules API Integration)
- Jules mode is enabled via `--jules-mode` flag or Jules backend configuration
- **Session Management**: Each issue processed by Jules gets a unique session ID
- **Cloud Tracking**: Session IDs are stored in `~/.auto-coder/<repo>/cloud.csv`
- **Issue Comments**: Jules automatically comments on issues with session ID
- **PR Feedback Loop**: Jules creates PRs which are detected and processed
- **CI Feedback**: When Jules PRs fail checks, error logs are sent back to Jules session

### New Components

1. **JulesClient** (`src/auto_coder/jules_client.py`)
   - Manages Jules sessions
   - Provides `start_session()`, `send_message()`, and `end_session()` methods
   - Automatically detects Jules CLI availability

2. **CloudManager** (`src/auto_coder/cloud_manager.py`)
   - Manages session tracking in `cloud.csv` files
   - Thread-safe operations
   - Bidirectional lookup (issue → session, session → issue)

3. **Jules PR Processing** (in `pr_processor.py`)
   - Detects Jules-created PRs (by author `google-labs-jules`)
   - Extracts session IDs from PR bodies
   - Links PRs to original issues
   - Sends CI failure feedback to Jules sessions

4. **Jules Issue Processing** (in `issue_processor.py`)
   - New `_process_issue_jules_mode()` function
   - Starts Jules sessions for issues
   - Stores session IDs in cloud.csv
   - Comments on issues with session information

### Configuration Changes

Jules is now included in the default backend configurations:

```toml
# In llm_config.toml, Jules is now a recognized backend
[backend]
default = "codex"
order = ["jules", "gemini", "qwen", "codex", "claude"]

[backends.jules]
enabled = true
# No API keys needed - uses Jules CLI with OAuth
backend_type = "jules"
```

## Who Is Affected

This breaking change affects:

1. **Users relying on Jules mode**: The internal implementation has changed
2. **Custom Jules integrations**: May need updates to use new APIs
3. **Scripts testing Jules functionality**: Test expectations may need updates

## Migration Steps

### For Users Using Jules Mode

**No action required** if you're using Jules mode through the standard CLI interface. The changes are mostly internal.

**If you're using Jules programmatically**, update your code to use the new API:

#### Before (Legacy):

```python
# Old approach - Jules label-based processing
from auto_coder.issue_processor import process_issue_jules_mode
result = process_issue_jules_mode(repo, issue_data, config, github)
```

#### After (New):

```python
# New approach - Jules API integration
from auto_coder.jules_client import JulesClient
from auto_coder.cloud_manager import CloudManager

# Start Jules session
jules = JulesClient()
session_id = jules.start_session(prompt)

# Store session mapping
cloud = CloudManager(repo_name)
cloud.add_session(issue_number, session_id)

# Comment on issue
github_client.add_comment_to_issue(
    repo_name,
    issue_number,
    f"Jules session started: {session_id}"
)
```

### For Jules Configuration

Jules is now automatically included in default configurations. To explicitly configure Jules:

```toml
# ~/.auto-coder/llm_config.toml

[backend]
# Add jules to your backend order if desired
order = ["jules", "gemini", "qwen", "codex"]

[backends.jules]
enabled = true
backend_type = "jules"
# No additional configuration needed
```

### Understanding Session Tracking

Jules sessions are now tracked in `cloud.csv` files:

```bash
# View session tracking for a repository
cat ~/.auto-coder/owner_repo/cloud.csv
```

Output format:
```csv
issue_number,session_id
123,session_abc123
456,session_xyz789
```

## New Features

### 1. Session Persistence

Sessions persist across application restarts via `cloud.csv`:

```python
# Retrieve session for an issue
cloud = CloudManager(repo_name)
session_id = cloud.get_session_id(issue_number)

# Reverse lookup: find issue by session
issue_number = cloud.get_issue_by_session(session_id)
```

### 2. Jules PR Detection

PRs created by Jules are automatically detected and processed:

- **Author Detection**: PRs by `google-labs-jules` are identified
- **Session Extraction**: Session IDs are extracted from PR bodies
- **Issue Linking**: PRs are linked to original issues
- **Body Updates**: PR bodies are updated to reference issues

### 3. CI Feedback Loop

When Jules PRs fail CI checks, error logs are automatically sent back to the Jules session:

```python
# This happens automatically in _send_jules_error_feedback()
# Error logs from GitHub Actions are formatted and sent to Jules
```

## Backward Compatibility

### Breaking Changes

1. **Legacy Jules label logic removed**: The old `jules` label-based processing has been removed
2. **API changes**: Jules-related APIs have changed
3. **Session format**: New session ID format and storage

### Compatibility Mode

There is **no backward compatibility mode** for this change. The Jules label logic has been completely removed as per the refactoring requirements.

## Testing

### Running Tests

Run the Jules-related tests to verify the implementation:

```bash
# Run Jules client tests
pytest tests/test_jules_client.py -v

# Run cloud manager tests
pytest tests/test_cloud_manager.py -v

# Run Jules integration tests
pytest tests/test_issue_processor_jules.py -v
pytest tests/test_pr_processor_jules.py -v
```

### Test Expectations

Tests have been updated to reflect the new session-based architecture. If you have custom tests, they may need updates to work with the new Jules APIs.

## Troubleshooting

### Issue: "Jules CLI not available"

**Solution**: Install and authenticate Jules CLI:
```bash
# Install Jules CLI (follow Jules documentation)
# Authenticate with Jules
jules auth login
```

### Issue: "No session ID found in Jules PR"

**Solution**: This is normal for PRs not created by Jules. Only PRs by `google-labs-jules` will have session IDs.

### Issue: "cloud.csv not found"

**Solution**: The cloud.csv file is created automatically when Jules sessions are started. It will be created at:
```
~/.auto-coder/<owner_repo>/cloud.csv
```

## Summary of Changes

| Aspect | Before | After |
|--------|--------|-------|
| **Jules Identification** | `jules` label | `--jules-mode` flag or Jules backend |
| **Session Management** | None | `cloud.csv` tracking |
| **Issue Comments** | None | Session ID commented automatically |
| **PR Processing** | Manual | Automatic Jules PR detection |
| **CI Feedback** | None | Automatic error feedback to Jules |
| **Configuration** | Not in backends | Full backend integration |

## Version Bump

This release bumps the version to **v2026.2.0.0** to reflect the breaking change nature of the Jules refactor.

## Support

If you encounter issues with this migration:

1. Check the test files for usage examples
2. Review the JulesClient and CloudManager APIs
3. Consult the example configuration in `docs/llm_backend_config.example.toml`

---

**Date:** 2025-11-29
**Issue:** #822
**Breaking Change:** YES

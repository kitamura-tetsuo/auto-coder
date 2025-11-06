# Implement Dependency Management for Issue Processing

## Overview

Implement dependency management functionality for the auto_coder issue processing system. The system should automatically check "Depends on" relationships and skip processing issues with unresolved dependencies.

## Detailed Requirements

### 1. Parse Dependencies
- Extract "Depends on" references from issue bodies using flexible regex patterns
- Handle variations like "Depends on: #233", "depends on #123", etc.
- Support both "Depends on" and "depends on" case variations

### 2. Dependency Resolution Check  
- Before processing any issue, check if all its dependencies are resolved (closed)
- Skip processing if any dependency is still open
- Ensure dependency checking happens early in the processing pipeline

### 3. Integration
- Integrate dependency checking into existing issue processing pipeline in `src/auto_coder/issue_processor.py`
- Update `src/auto_coder/github_client.py` to support dependency resolution queries
- Ensure compatibility with existing sub-issue handling via `github-sub-issue` commands

### 4. Technical Implementation

**Expected Files to Modify (8 files):**
- `src/auto_coder/issue_processor.py` - Main dependency check logic
- `src/auto_coder/github_client.py` - Enhanced issue query methods  
- `tests/test_issue_processor_skip_linked.py` - Extend existing tests
- `tests/test_github_client.py` - Add dependency resolution tests
- `docs/client-features.yaml` - Document new dependency feature
- `src/auto_coder/prompts.yaml` - Update processing instructions
- `src/auto_coder/pr_processor.py` - Ensure dependency awareness in PR processing
- `src/auto_coder/config.py` - Add configuration options for dependency handling

### 5. Edge Cases to Handle
- Circular dependencies detection
- Invalid issue references (non-existent issue numbers)
- Mixed dependency formats in same issue
- Dependencies on issues in different repositories

## Acceptance Criteria
- [ ] Issues with unresolved dependencies are automatically skipped
- [ ] Regex pattern handles various "Depends on" formats (case-insensitive, with/without colon)
- [ ] All existing tests continue to pass
- [ ] New tests validate dependency resolution behavior
- [ ] Integration works seamlessly with existing github-sub-issue workflow
- [ ] Proper error handling for invalid dependency references

## Implementation Notes
- Follow existing code patterns in the auto_coder codebase
- Ensure backward compatibility with existing issue processing
- Add comprehensive logging for debugging dependency resolution issues
- Consider performance impact of dependency checking on large issue lists
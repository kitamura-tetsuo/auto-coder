# Migrate Unpushed Commit Checks into branch_context for Issue Processing

## Problem Description
Currently, "Checking for unpushed commits before processing issue" is performed separately before branch switching operations. This scattered approach makes the issue processing workflow harder to maintain and understand.

## Current State
- Unpushed commit checks are performed in `issue_processor.py` in the `_apply_issue_actions_directly` function (lines 296-304)
- Branch switching logic is handled separately through the `branch_context` context manager
- These operations are conceptually related but implemented in different places

## Desired State
Move all unpushed commit checking logic into the `branch_context` context manager in `git_utils.py` so that:
1. Unpushed commits are automatically checked when entering any branch context
2. Issue processing becomes cleaner and more maintainable
3. The branch context management is more comprehensive

## Implementation Plan

This issue has been broken down into 3 sub-issues for better manageability:

1. **Sub-Issue 1: Core branch_context Migration** - Migrate unpushed commit logic into branch_context core
2. **Sub-Issue 2: Update Issue Processing Logic** - Update issue_processor.py to use the enhanced branch_context
3. **Sub-Issue 3: Update PR Processing Logic** - Update PR processing to use enhanced branch_context and ensure compatibility

## Success Criteria
- [ ] All unpushed commit checks moved into branch_context context manager
- [ ] Issue processing works correctly with the migrated logic
- [ ] PR processing continues to work without regressions
- [ ] Code is more maintainable and follows single responsibility principle
- [ ] All existing tests pass

## Dependencies
This main issue depends on completion of all 3 sub-issues.

---
*This issue was generated automatically for code improvement and maintainability.*
# Sub-Issue 1: Migrate Unpushed Commit Logic into branch_context Core

## Description
Migrate the unpushed commit checking logic from `issue_processor.py` into the `branch_context` context manager in `git_utils.py` to centralize branch management operations.

## Current Implementation Location
- `issue_processor.py` lines 296-304: `with ProgressStage("Checking unpushed commits")` in `_apply_issue_actions_directly`

## Tasks
1. **Enhance branch_context in git_utils.py:**
   - Add unpushed commit check at context entry
   - Add automatic push functionality if unpushed commits exist
   - Handle different remote configurations
   - Add appropriate logging and progress tracking

2. **Update function signature if needed:**
   - Add optional parameters for unpushed commit behavior
   - Maintain backward compatibility

3. **Test the enhanced branch_context:**
   - Verify existing branch switching still works
   - Test unpushed commit detection and handling
   - Ensure proper error handling

## Files to Modify
1. `src/auto_coder/git_utils.py` - Main modifications to `branch_context` function
2. New test files to verify the enhanced functionality

## Success Criteria
- [ ] branch_context automatically checks for unpushed commits on entry
- [ ] Unpushed commits are automatically pushed when entering branch context
- [ ] Proper logging and error handling for unpushed commit operations
- [ ] Backward compatibility maintained
- [ ] All existing tests pass

## Dependencies
This is Sub-Issue 1 of the main "Migrate Unpushed Commit Checks into branch_context" issue. No dependencies on other sub-issues.

---
*Generated for sub-issue 1/3 of branch context migration project.*
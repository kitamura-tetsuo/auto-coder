# Sub-Issue 2: Update Issue Processing Logic for Enhanced branch_context

## Description
Update the issue processing logic in `issue_processor.py` to remove the duplicated unpushed commit checking code and leverage the enhanced `branch_context` functionality.

## Current Implementation Location
- `issue_processor.py` lines 296-304: `_apply_issue_actions_directly` function
- Current logic performs unpushed commit check before entering branch context

## Tasks
1. **Remove duplicated unpushed commit logic:**
   - Remove the `with ProgressStage("Checking unpushed commits")` block from `_apply_issue_actions_directly`
   - Remove the `ensure_pushed()` call and related error handling

2. **Update issue processing flow:**
   - Ensure issue processing works seamlessly with enhanced branch_context
   - Update any references or dependencies on the old unpushed commit checking
   - Maintain proper error handling and logging

3. **Update progress tracking:**
   - Remove or update progress stages related to unpushed commit checking
   - Ensure progress reporting remains accurate

4. **Test issue processing flow:**
   - Verify issue processing works with the migrated logic
   - Test both successful and error scenarios
   - Ensure no regressions in issue handling

## Files to Modify
1. `src/auto_coder/issue_processor.py` - Remove duplicated unpushed commit logic from `_apply_issue_actions_directly`
2. Update related test files to reflect the changes

## Success Criteria
- [ ] Duplicated unpushed commit checking code removed from issue_processor.py
- [ ] Issue processing works correctly with enhanced branch_context
- [ ] No regressions in issue processing functionality
- [ ] Progress tracking remains accurate
- [ ] All existing issue processing tests pass

## Dependencies
Depends on completion of Sub-Issue 1 (branch_context core migration). Should be completed before Sub-Issue 3.

---
*Generated for sub-issue 2/3 of branch context migration project.*
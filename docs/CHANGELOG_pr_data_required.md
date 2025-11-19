# Make pr_data Parameter Required in Conflict Resolution

## Overview
This change makes the `pr_data` parameter mandatory in the `_perform_base_branch_merge_and_conflict_resolution` function to improve type safety and prevent None-related bugs.

## Key Changes

### 1. Function Signature Update

#### Before
```python
def _perform_base_branch_merge_and_conflict_resolution(
    pr_number: int,
    base_branch: str,
    config: AutomationConfig,
    repo_name: Optional[str] = None,
    pr_data: Optional[Dict[str, Any]] = None,
) -> bool:
```

#### After
```python
def _perform_base_branch_merge_and_conflict_resolution(
    pr_number: int,
    base_branch: str,
    config: AutomationConfig,
    pr_data: Dict[str, Any],
    repo_name: Optional[str] = None,
) -> bool:
```

### 2. Parameter Reordering
- Moved required `pr_data` parameter before optional `repo_name` parameter
- This follows Python's requirement that required parameters must come before optional parameters with defaults

### 3. Removed Default Value Logic
- Removed the fallback code that created a default `pr_data` dictionary when None was passed
- Removed this code block:
  ```python
  if pr_data is None:
      pr_data = {"number": pr_number, "base_branch": base_branch}
  ```
- Now simply ensures the base_branch is set: `pr_data = {**pr_data, "base_branch": base_branch}`

### 4. Updated Function Calls

#### In conflict_resolver.py (line 292)
**Before:**
```python
return _perform_base_branch_merge_and_conflict_resolution(pr_number, base_branch, config, repo_name, pr_data)
```

**After:**
```python
return _perform_base_branch_merge_and_conflict_resolution(pr_number, base_branch, config, pr_data, repo_name)
```

#### In pr_processor.py (lines 663-669)
**Before:**
```python
conflict_resolved = _perform_base_branch_merge_and_conflict_resolution(
    pr_number,
    target_branch,
    config,
    repo_name,
    pr_data,
)
```

**After:**
```python
conflict_resolved = _perform_base_branch_merge_and_conflict_resolution(
    pr_number,
    target_branch,
    config,
    pr_data,
    repo_name,
)
```

## Benefits

1. **Type Safety** - Ensures `pr_data` is always provided, preventing None-related bugs
2. **Clearer API** - Makes the dependency explicit in the function signature
3. **Better IDE Support** - IDEs can now properly infer and validate pr_data usage
4. **Easier Maintenance** - Removes conditional logic for handling None values
5. **Consistency** - Aligns with other functions that already require pr_data

## Testing

All function calls have been verified:
- Existing calls in both files pass pr_data as a dictionary
- No call sites needed to be modified beyond parameter reordering
- Type checkers (Pylance) confirm type correctness

## Migration Guide

For any future code calling this function:

```python
# Required
result = _perform_base_branch_merge_and_conflict_resolution(
    pr_number=123,
    base_branch="main",
    config=automation_config,
    pr_data={"number": 123, "title": "Test PR"},  # Must provide pr_data
    repo_name="owner/repo"  # Optional
)
```

## Files Modified

- `src/auto_coder/conflict_resolver.py` - Function signature and one call site
- `src/auto_coder/pr_processor.py` - Function call site
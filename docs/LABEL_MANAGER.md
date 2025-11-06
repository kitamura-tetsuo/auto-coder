# LabelManager Context Manager

The `LabelManager` is a context manager class that provides unified @auto-coder label operations with proper resource cleanup, retry logic, and error handling.

## Overview

The `LabelManager` class was created to replace scattered label management code across multiple files. It provides a clean, Pythonic interface for managing GitHub issue and PR labels with the following features:

- **Context Manager**: Automatic label addition on entry and removal on exit
- **Retry Logic**: Exponential backoff retry for API failures
- **Error Handling**: Graceful handling of API errors with proper logging
- **Resource Cleanup**: Guaranteed label removal even on exceptions
- **Thread Safety**: Each instance maintains isolated state
- **Configuration**: Support for dry run, label disable, and custom label names

## Usage

### Basic Usage

```python
from src.auto_coder.label_manager import LabelManager

# Use LabelManager as a context manager
with LabelManager(
    github_client=github_client,
    repo_name="owner/repo",
    item_number=123,
    item_type="issue",
    config=config,
) as lm:
    # The @auto-coder label is automatically added
    # Process your issue/PR here
    process_issue()
# Label is automatically removed when exiting the context
```

### Handling Race Conditions

```python
from src.auto_coder.label_manager import LabelManager, LabelOperationError

try:
    with LabelManager(
        github_client=github_client,
        repo_name="owner/repo",
        item_number=123,
    ) as lm:
        # Process issue
        pass
except LabelOperationError as e:
    # Another instance is already processing this item
    print(f"Skipping: {e}")
```

### Custom Label Name

```python
# Use a custom label instead of @auto-coder
with LabelManager(
    github_client=github_client,
    repo_name="owner/repo",
    item_number=123,
    label_name="work-in-progress",
) as lm:
    # Process with custom label
    pass
```

### Dry Run Mode

```python
# Test without making actual API calls
with LabelManager(
    github_client=github_client,
    repo_name="owner/repo",
    item_number=123,
    dry_run=True,
) as lm:
    # Simulate processing
    pass
# No actual label operations performed
```

### Verifying Label Exists

```python
lm = LabelManager(
    github_client=github_client,
    repo_name="owner/repo",
    item_number=123,
)

if lm.verify_label_exists():
    print("Label is present")
else:
    print("Label is not present")
```

## API Reference

### LabelManager

```python
class LabelManager:
    def __init__(
        self,
        github_client: Any,
        repo_name: str,
        item_number: Union[int, str],
        item_type: str = "issue",
        dry_run: bool = False,
        config: Any = None,
        label_name: str = "@auto-coder",
        max_retries: int = 3,
        retry_delay: float = 0.5,
    )
```

**Parameters:**
- `github_client`: GitHub client instance
- `repo_name`: Repository name in format "owner/repo"
- `item_number`: Issue or PR number
- `item_type`: Type of item ('issue' or 'pr')
- `dry_run`: If True, skip actual label operations
- `config`: AutomationConfig instance (optional)
- `label_name`: Name of the label to manage (default: '@auto-coder')
- `max_retries`: Maximum number of retry attempts for API failures (default: 3)
- `retry_delay`: Delay in seconds between retries (default: 0.5)

**Methods:**

- `verify_label_exists() -> bool`: Check if label exists on issue/PR (private helper)

**Context Manager Protocol:**

- `__enter__() -> bool`: Adds label and returns True if processing should continue
- `__exit__(exc_type, exc_val, exc_tb)`: Removes label and cleans up

### LabelOperationError

Exception raised when label operations fail or another instance is processing the item.

## Legacy Function Removal

The legacy utility functions `check_and_add_label()`, `remove_label()`, and `check_label_exists()` have been removed.
Please use the `LabelManager` context manager instead, which provides a cleaner and more reliable API for label management.

```python
# Use LabelManager context manager
from src.auto_coder.label_manager import LabelManager

with LabelManager(github_client, "owner/repo", 123, item_type="issue", config=config) as should_process:
    if not should_process:
        return  # Another instance is processing

    # Process issue
    # Label is automatically managed
# Label automatically removed on exit
```

## Benefits

1. **Reduced Code**: Eliminates try/finally blocks for cleanup
2. **Better Safety**: Guaranteed cleanup even on exceptions
3. **Race Detection**: Automatic detection of concurrent processing
4. **Retry Logic**: Built-in retry with exponential backoff
5. **Maintainability**: Single source of truth for label operations
6. **Type Safety**: Clear type hints and documentation
7. **Testability**: Easy to test with mocked GitHub client

## Performance

- Minimal overhead: one additional object allocation

## Implementation Details

### Thread Safety

Each LabelManager instance maintains isolated state:
- `_label_added`: Tracks if label was added by this instance
- `_should_cleanup`: Tracks if cleanup is needed
- `_labels_disabled`: Cached label disable state

### Label Disable Support

Labels can be disabled in two ways:
1. GitHub client attribute: `github_client.disable_labels = True`
2. Config attribute: `config.DISABLE_LABELS = True`

When disabled, all label operations become no-ops but processing continues.

## Testing

Comprehensive tests are provided in `tests/test_label_manager.py`:

- Context manager lifecycle
- Label addition and removal
- Race condition handling
- Dry run mode
- Label disable functionality
- Retry mechanism
- Exception handling and cleanup
- Custom label names
- Backward compatibility
- Thread safety

Run tests with:
```bash
bash scripts/test.sh tests/test_label_manager.py
```

## Migration Strategy

1. **Phase 1**: LabelManager is available for new code
2. **Phase 2**: Deprecation warnings on old functions (current)
3. **Phase 3**: Old functions can be removed in a future version

### Updating Existing Code

To update existing code to use LabelManager:

**Before:**
```python
def process_issue(github_client, repo_name, issue_number, config):
    # Add label
    if not check_and_add_label(github_client, repo_name, issue_number, "issue", False, config):
        return  # Skip, another instance processing

    try:
        # Process issue
        do_work()
    finally:
        # Remove label
        remove_label(github_client, repo_name, issue_number, "issue", False, config)
```

**After:**
```python
def process_issue(github_client, repo_name, issue_number, config):
    try:
        with LabelManager(github_client, repo_name, issue_number, "issue", False, config) as lm:
            # Process issue - label guaranteed to be present
            do_work()
    except LabelOperationError:
        return  # Skip, another instance processing
    # Label automatically removed
```

## Benefits

1. **Reduced Code**: Eliminates try/finally blocks for cleanup
2. **Better Safety**: Guaranteed cleanup even on exceptions
3. **Race Detection**: Automatic detection of concurrent processing
4. **Retry Logic**: Built-in retry with exponential backoff
5. **Maintainability**: Single source of truth for label operations
6. **Type Safety**: Clear type hints and documentation
7. **Testability**: Easy to test with mocked GitHub client

## Performance

- Minimal overhead: one additional object allocation
- Cached label disable state: no repeated checks
- Efficient retry: exponential backoff prevents API overload
- No locks required: state is per-instance, avoiding contention

## Files Modified

- `src/auto_coder/label_manager.py`: Added LabelManager class and deprecated old functions
- `tests/test_label_manager.py`: Comprehensive test suite

## Future Enhancements

Potential future improvements:
1. Metrics collection (label operation timing, retry counts)
2. Async/await support for async GitHub clients
3. Batch label operations for multiple items
4. Custom retry strategies (linear backoff, fixed delay)
5. Integration with existing progress tracking

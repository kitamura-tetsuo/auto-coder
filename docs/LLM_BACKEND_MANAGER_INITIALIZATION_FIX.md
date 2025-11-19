# Fix LLM Backend Manager Initialization Error

## Problem
- `LLMBackendManager.get_llm_instance()` initialization error occurred in `pr_processor.py:1275` when calling `get_llm_backend_manager().run_test_fix_prompt()`
- The error message: "LLMBackendManager.get_llm_instance() must be called with initialization parameters (default_backend, default_client, factories) on first use or when force_reinitialize=True"

## Root Cause
- CLI commands were creating BackendManager instances using `build_backend_manager_from_config()` but not initializing the LLMBackendManager singleton
- When `pr_processor.py` tried to access the singleton via `get_llm_backend_manager()`, it found an uninitialized singleton and threw an error

## Solution
### 1. Initialize LLM Backend Manager Singleton in CLI Commands
Added singleton initialization after Manager creation in all three main CLI commands:

**Files Modified:**
- `src/auto_coder/cli_commands_main.py`

**Changes:**
Added the following initialization code after `build_backend_manager_from_config()` call in:
- `process_issues` command
- `create_feature_issues` command  
- `fix_to_pass_tests` command

```python
# Initialize LLM backend manager singleton
from .backend_manager import LLMBackendManager
LLMBackendManager.get_llm_instance(
    default_backend=manager._default_backend,
    default_client=manager._clients[manager._default_backend],
    factories=manager._factories,
    order=manager._all_backends,
)
```

### 2. Improve Type Safety
Enhanced type checking in `_extract_backend_model()` function:

**Files Modified:**
- `src/auto_coder/fix_to_pass_tests_runner.py`

**Changes:**
Added tuple type validation to prevent Pylance errors:

```python
result = getter()
if isinstance(result, tuple) and len(result) == 2:
    backend, model = result
    # ... rest of logic
```

## Impact
- ✅ All CLI commands can now properly use LLM backend functionality
- ✅ PR processing with local test fixes works correctly
- ✅ Test-driven development flow stability improved
- ✅ All backend manager tests pass (13/13)
- ✅ Pylance type errors resolved

## Test Results
- All backend manager singleton tests pass
- CLI commands execute successfully
- No Pylance errors in the affected files

## Related Files
- `src/auto_coder/cli_commands_main.py` - Main CLI command definitions
- `src/auto_coder/fix_to_pass_tests_runner.py` - Test execution and fix functionality
- `src/auto_coder/pr_processor.py` - PR processing logic (no changes needed)
- `src/auto_coder/backend_manager.py` - Backend manager implementation (no changes needed)
# Migration Guide: Issue #909 - Backend Manager API Renaming

## Overview

This migration guide covers the breaking changes introduced in issue #909, which renames backend manager APIs to use consistent "noedit" terminology instead of "message" terminology.

## Breaking Changes

### 1. Global Instance Variable

**Before:**
```python
# Internal variable (not typically used directly)
_message_instance
```

**After:**
```python
# Internal variable (not typically used directly)
_noedit_instance
```

**Impact:** This is an internal variable and should not affect external users.

---

### 2. Function Renames

#### `get_message_backend_manager()` → `get_noedit_backend_manager()`

**Before:**
```python
from auto_coder.backend_manager import get_message_backend_manager

manager = get_message_backend_manager(
    default_backend="codex",
    default_client=client,
    factories={"codex": lambda: client}
)
```

**After:**
```python
from auto_coder.backend_manager import get_noedit_backend_manager

manager = get_noedit_backend_manager(
    default_backend="codex",
    default_client=client,
    factories={"codex": lambda: client}
)
```

**Deprecated Alias Available:** Yes, `get_message_backend_manager()` still works but emits a deprecation warning.

---

#### `run_llm_message_prompt()` → `run_llm_noedit_prompt()`

**Before:**
```python
from auto_coder.backend_manager import run_llm_message_prompt

response = run_llm_message_prompt("Generate commit message")
```

**After:**
```python
from auto_coder.backend_manager import run_llm_noedit_prompt

response = run_llm_noedit_prompt("Generate commit message")
```

**Deprecated Alias Available:** Yes, `run_llm_message_prompt()` still works but emits a deprecation warning.

---

#### `get_message_backend_and_model()` → `get_noedit_backend_and_model()`

**Before:**
```python
from auto_coder.backend_manager import get_message_backend_and_model

backend, model = get_message_backend_and_model()
```

**After:**
```python
from auto_coder.backend_manager import get_noedit_backend_and_model

backend, model = get_noedit_backend_and_model()
```

**Deprecated Alias Available:** Yes, `get_message_backend_and_model()` still works but emits a deprecation warning.

---

## Migration Strategy

### Immediate Action Required

**None.** All deprecated functions continue to work with deprecation warnings.

### Recommended Migration Path

1. **Update imports** to use new function names:
   ```python
   # Old
   from auto_coder.backend_manager import (
       get_message_backend_manager,
       run_llm_message_prompt,
       get_message_backend_and_model
   )

   # New
   from auto_coder.backend_manager import (
       get_noedit_backend_manager,
       run_llm_noedit_prompt,
       get_noedit_backend_and_model
   )
   ```

2. **Update function calls** throughout your codebase:
   ```bash
   # Find all occurrences
   grep -r "get_message_backend_manager" .
   grep -r "run_llm_message_prompt" .
   grep -r "get_message_backend_and_model" .
   ```

3. **Test your changes** to ensure everything works correctly.

### Automated Migration

You can use the following sed commands for bulk replacement:

```bash
# For Python files
find . -name "*.py" -type f -exec sed -i \
    -e 's/get_message_backend_manager/get_noedit_backend_manager/g' \
    -e 's/run_llm_message_prompt/run_llm_noedit_prompt/g' \
    -e 's/get_message_backend_and_model/get_noedit_backend_and_model/g' \
    {} +
```

**Warning:** Always review automated changes and test thoroughly.

---

## Deprecation Timeline

- **Current Release:** Deprecated functions available with warnings
- **Future Release (TBD):** Deprecated functions will be removed

**Recommendation:** Migrate to new API names as soon as possible to avoid issues when deprecated functions are removed.

---

## Rationale

This change improves API consistency by aligning the backend manager function names with the configuration schema changes in issue #908, where `message_backend` was renamed to `backend_for_noedit`.

The term "noedit" more accurately describes the purpose: these backends are used for non-editing operations like commit messages and PR descriptions, as opposed to the main LLM backend used for code editing.

---

## Support

If you encounter any issues during migration:

1. Check that you're using the latest version of auto-coder
2. Review the deprecation warnings in your logs
3. Report issues at: https://github.com/kitamura-tetsuo/auto-coder/issues

---

## Related Issues

- **Parent Issue:** #906
- **Depends On:** #908
- **Current Issue:** #909

# Qdrant Collection Separation System - Implementation Summary

## Overview
Implemented repository-hashed collection system to prevent data contamination when multiple auto-coder instances run simultaneously. Each repository now gets its own isolated Qdrant collection.

## Changes Made

### 1. GraphRAGIndexManager (`src/auto_coder/graphrag_index_manager.py`)

#### Added Method: `_get_repository_collection_name()`
- Generates unique collection name for repository using SHA256 hash
- Format: `repo_{hash[:16]}` (e.g., `repo_a1b2c3d4e5f6g7h8`)
- Uses resolved absolute path for consistent hashing
- Handles symlinks correctly by resolving to real path

#### Modified Method: `_store_embeddings_in_qdrant()`
- **Line 542-544**: Changed from hardcoded `collection_name = "code_embeddings"`
- Now uses: `collection_name = self._get_repository_collection_name()`
- Adds logging to show which collection is being used

### 2. CodeAnalysisTool (`src/auto_coder/mcp_servers/graphrag_mcp/graphrag_mcp/code_analysis_tool.py`)

#### Modified Constructor: `__init__()`
- **Line 20**: Added optional `collection_name` parameter
- **Line 30-31**: Uses provided collection_name or environment variable or default "code_chunks"
- Ensures backward compatibility

#### Modified Method: `semantic_code_search()`
- **Line 413-415**: Added optional `collection_name` parameter
- **Line 448-449**: Uses provided collection_name or falls back to instance default
- Maintains backward compatibility when collection_name not specified

### 3. GraphRAGMCPIntegration (`src/auto_coder/graphrag_mcp_integration.py`)

#### Added Method: `get_repository_collection_name()`
- Exposes repository-specific collection name for current repository
- Delegates to index_manager's method
- Enables external code to retrieve collection name

#### Modified Method: `get_mcp_config_for_llm()`
- **Line 268-269**: Now includes `qdrant_collection` in returned config
- Passes repository-specific collection name to LLM clients
- Helps coordinate between indexing and searching

### 4. Test Suite (`tests/test_graphrag_index_manager.py`)

#### Added Tests:
1. `test_get_repository_collection_name()` - Verifies correct format and generation
2. `test_get_repository_collection_name_consistent()` - Ensures consistency for same repo
3. `test_get_repository_collection_name_different_repos()` - Different repos get different names
4. `test_get_repository_collection_name_with_symlink()` - Symlinks resolve correctly

## Technical Details

### Hash Generation Strategy
```python
repo_hash = hashlib.sha256(str(repo_path.resolve()).encode()).hexdigest()[:16]
return f"repo_{repo_hash}"
```

### Benefits
1. **Isolation**: Each repository has completely separate vector storage
2. **Consistency**: Same repository always uses same collection name
3. **Scalability**: Multiple repositories can be indexed without conflict
4. **Backward Compatibility**: Existing code continues to work
5. **Symlink Support**: Resolves symlinks to prevent duplicate collections

### Collection Lifecycle
1. **Creation**: During first index for repository
2. **Update**: When codebase changes are detected
3. **Persistence**: Collection persists across sessions
4. **Cleanup**: Manual or automated (future enhancement)

## Usage Examples

### Indexing (Automatic)
```python
manager = GraphRAGIndexManager(repo_path="/path/to/repo")
manager.update_index()  # Uses repo-specific collection
# Creates collection: repo_a1b2c3d4e5f6g7h8
```

### Searching (Manual Specification)
```python
# Method 1: Specify during initialization
tool = CodeAnalysisTool(collection_name="repo_a1b2c3d4e5f6g7h8")
results = tool.semantic_code_search("hash functions")

# Method 2: Specify per search
tool = CodeAnalysisTool()  # Uses default
results = tool.semantic_code_search(
    "hash functions",
    collection_name="repo_a1b2c3d4e5f6g7h8"
)
```

### Integration
```python
integration = GraphRAGMCPIntegration()
collection_name = integration.get_repository_collection_name()
# Returns: "repo_a1b2c3d4e5f6g7h8"
```

## Backward Compatibility

1. **CodeAnalysisTool**: Defaults to environment variable or "code_chunks" if no collection_name specified
2. **semantic_code_search**: Works with existing code that doesn't pass collection_name
3. **GraphRAGMCPIntegration**: Existing code continues to work, new field added to config

## Testing

All new functionality has corresponding unit tests:
- Collection name generation
- Consistency checks
- Different repository handling
- Symlink resolution

Tests verify:
- Correct format (repo_{16-char-hash})
- Deterministic generation (same repo = same name)
- Isolation (different repos = different names)
- Symlink handling (symlinks resolve to same as real path)

## Acceptance Criteria Status

- ✅ Each repository gets unique Qdrant collection name
- ✅ Collection names are consistent between indexing and searching
- ✅ Repository-hashed collection system implemented
- ✅ Tests added for new functionality
- ✅ Backward compatibility maintained
- ✅ Minimal performance impact (single hash calculation per operation)

## Future Enhancements (Sub-Issue 2)

The issue mentions collection cleanup for moved/deleted repositories. This can be implemented in a future iteration by:
1. Tracking all created collections in a registry
2. Detecting when repositories are moved/deleted
3. Cleaning up orphaned collections

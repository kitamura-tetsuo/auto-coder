# GraphRAG Repository Isolation Architecture

## Overview

The repository isolation system ensures complete data separation between different code repositories when using GraphRAG MCP.

## Architecture Components

### 1. Qdrant Collection Isolation
- Each repository gets unique collection name: `repo_{hash}`
- Hash generated from absolute repository path
- Complete semantic search isolation

### 2. Neo4j Node Labeling
- Repository-specific labels: `Repo_{HASH}:CodeNode`
- Graph queries automatically filtered by repository
- Relationship analysis scoped to repository

### 3. MCP Session Management
- Unique session IDs for each auto-coder instance
- Automatic session lifecycle management
- Thread-safe concurrent session handling

## Data Flow

```
┌─────────────────┐     ┌─────────┐     ┌──────────┐
│ Auto-Coder      │────▶│ MCP     │────▶│ Qdrant   │
│ Instance 1      │     │ Server  │     │          │
│ (Session 1)     │     │         │     │ Collection│
└─────────────────┘     └─────────┘     │ repo_abc │
                                      └──────────┘
                                             │
                                      ┌──────────┐
                                      │ Neo4j    │
                                      │ Label    │
                                      │ Repo_ABC │
                                      └──────────┘

┌─────────────────┐     ┌─────────┐     ┌──────────┐
│ Auto-Coder      │────▶│ MCP     │────▶│ Qdrant   │
│ Instance 2      │     │ Server  │     │          │
│ (Session 2)     │     │         │     │ Collection│
└─────────────────┘     └─────────┘     │ repo_def │
                                      └──────────┘
                                             │
                                      ┌──────────┐
                                      │ Neo4j    │
                                      │ Label    │
                                      │ Repo_DEF │
                                      └──────────┘
```

## API Reference

### New MCP Tools

#### `set_repository_context()`
Set working repository context for session.

**Parameters:**
- `repo_path` (string): Path to repository
- `session_id` (string, optional): Unique session identifier

**Returns:**
```json
{
  "status": "ok",
  "session_id": "abc123",
  "repository": "/path/to/repo",
  "collection_name": "repo_abc123",
  "created_at": "2025-10-31T14:05:00Z"
}
```

### Enhanced Existing Tools

All existing MCP tools now accept optional `session_id` parameter:

#### `find_symbol(fqname, session_id=None)`
- `fqname`: Symbol name to find
- `session_id`: Repository context (optional)

#### `semantic_code_search(query, session_id=None, limit=10)`
- `query`: Search query
- `session_id`: Repository context (optional)
- `limit`: Maximum results (optional)

#### `get_call_graph(symbol_id, session_id=None, direction='both', depth=1)`
- `symbol_id`: Symbol to analyze
- `session_id`: Repository context (optional)
- `direction`: 'callers', 'callees', or 'both'
- `depth`: Analysis depth 1-3

## Session Management

### Creating a Session

```python
from auto_coder.graphrag_mcp_integration import GraphRAGMCPIntegration

# Create integration instance
integration = GraphRAGMCPIntegration()

# Create session for specific repository
session_id = integration.create_session("/path/to/your/repo")
```

### Using Sessions

```python
# Set session context
integration.set_repository_context("/path/to/repo", session_id)

# All operations now scoped to repository
result = integration.mcp_tool.find_symbol("function_name", session_id=session_id)

# Multiple operations share same context
results = integration.mcp_tool.semantic_code_search(
    "user authentication",
    session_id=session_id
)
```

### Session Lifecycle

```python
# Check session status
status = integration.get_session_info(session_id)

# List all active sessions
sessions = integration.list_active_sessions()

# Cleanup expired sessions
integration.cleanup_expired_sessions(max_age_hours=24)
```

## Best Practices

### 1. Session Management
- Create a session per repository
- Reuse sessions for multiple operations
- Clean up sessions when done

### 2. Error Handling
- Always provide session_id for repository-specific operations
- Handle session expiration errors
- Implement retry logic for failed operations

### 3. Performance Optimization
- Use session pooling for high-frequency operations
- Batch operations within a session
- Monitor session count to prevent resource exhaustion

### 4. Multi-Repository Workflows
```python
# Process multiple repositories
repos = ["/path/to/repo1", "/path/to/repo2", "/path/to/repo3"]
sessions = {}

for repo in repos:
    # Create isolated session
    session_id = integration.create_session(repo)
    sessions[repo] = session_id

    # Process repository
    result = mcp_tool.analyze_repository(repo, session_id=session_id)

# Cleanup
for repo, session_id in sessions.items():
    integration.cleanup_session(session_id)
```

## Migration from v1.x

### Automatic Migration
The system automatically handles migration when you upgrade to v2.0+:

```bash
auto-coder graphrag migrate-to-isolation --backup
```

### Manual Migration
For advanced scenarios:

```python
# Check current state
status = check_migration_readiness()

if status['ready']:
    # Backup existing data
    backup_id = create_backup()

    # Perform migration
    result = perform_migration()

    # Verify results
    verify_migration()
```

## Limitations and Known Issues

1. **Session Limit**: Maximum 100 concurrent sessions per MCP server
2. **Backward Compatibility**: Legacy mode shows deprecation warnings
3. **Memory Usage**: Each session consumes additional memory
4. **Migration Time**: Large repositories may take longer to migrate

## Support

For issues with repository isolation:
1. Check the migration guide
2. Review troubleshooting section
3. Enable verbose logging: `--verbose`
4. Report issues with session info and error logs

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
┌─────────────────────┐      Session 1      ┌──────────────────┐
│ Auto-Coder Instance ├────────────────────→│   MCP Server     │
│         1           │                      └────────┬─────────┘
└─────────────────────┘                                │
                                                       │ Collection: repo_abc123
┌─────────────────────┐      Session 2      ┌────────▼─────────┐
│ Auto-Coder Instance ├────────────────────→│   Qdrant         │
│         2           │                      └────────┬─────────┘
└─────────────────────┘                               │
                                                       │ Label: Repo_ABC123
┌─────────────────────┐      Session 3      ┌────────▼─────────┐
│ Auto-Coder Instance ├────────────────────→│   Neo4j          │
│         3           │                      └─────────────────┘
└─────────────────────┘
```

## Collection Naming Strategy

### Qdrant Collections
Each repository creates a unique collection name using the format:

```
repo_{repository_hash}
```

Where `repository_hash` is a SHA-256 hash of the absolute repository path.

Example:
- Repository: `/home/user/projects/my-app`
- Hash: `abc123def456`
- Collection Name: `repo_abc123def456`

### Neo4j Labels
Nodes in Neo4j are labeled with repository-specific labels:

```
Repo_{REPOSITORY_HASH}:CodeNode
```

Example:
- Repository: `/home/user/projects/my-app`
- Hash: `ABC123DEF456` (uppercase)
- Label: `Repo_ABC123DEF456:CodeNode`

## Session Management

### Session Lifecycle

1. **Creation**: Session created when `set_repository_context()` is called
2. **Active**: Session used for all MCP tool calls within the context
3. **Cleanup**: Sessions can be manually cleaned up or automatically expire

### Session Storage

Sessions are stored in memory with the following information:

```python
{
    "session_id": "unique-session-id",
    "repository_path": "/path/to/repo",
    "repository_hash": "abc123def456",
    "collection_name": "repo_abc123def456",
    "created_at": "2025-10-31T14:05:00Z",
    "last_used": "2025-10-31T14:10:00Z",
    "expires_at": "2025-11-01T14:10:00Z"
}
```

## Query Isolation

### Semantic Search (Qdrant)

When performing semantic search, the system automatically:

1. Uses repository-specific collection name
2. Filters results to current repository context
3. Returns only symbols from the current repository

### Graph Queries (Neo4j)

Graph queries automatically:

1. Filter by repository label
2. Scope relationships to repository
3. Return repository-specific call graphs

### Example Queries

#### Find Symbol
```python
# Session 1 - Repository A
session_id = mcp_tool.set_repository_context("/repo/project-a")
result = mcp_tool.find_symbol("calculate_total", session_id=session_id)
# Returns: Symbol from Project A only

# Session 2 - Repository B
session_id = mcp_tool.set_repository_context("/repo/project-b")
result = mcp_tool.find_symbol("calculate_total", session_id=session_id)
# Returns: Symbol from Project B only
```

#### Semantic Code Search
```python
# Repository A context
session_a = mcp_tool.set_repository_context("/repo/project-a")
results = mcp_tool.semantic_code_search(
    "user authentication",
    session_id=session_a,
    limit=10
)
# Returns: Only results from Project A

# Repository B context
session_b = mcp_tool.set_repository_context("/repo/project-b")
results = mcp_tool.semantic_code_search(
    "user authentication",
    session_id=session_b,
    limit=10
)
# Returns: Only results from Project B
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

**Example:**
```python
session_id = mcp_tool.set_repository_context("/path/to/repo")
result = mcp_tool.find_symbol("function_name", session_id=session_id)
```

#### `semantic_code_search(query, session_id=None, limit=10)`
- `query`: Search query
- `session_id`: Repository context (optional)
- `limit`: Maximum results (optional)

**Example:**
```python
session_id = mcp_tool.set_repository_context("/path/to/repo")
results = mcp_tool.semantic_code_search(
    "user authentication",
    session_id=session_id,
    limit=10
)
```

#### `get_call_graph(symbol_id, session_id=None, direction='both', depth=1)`
- `symbol_id`: Symbol to analyze
- `session_id`: Repository context (optional)
- `direction`: 'callers', 'callees', or 'both'
- `depth`: Analysis depth 1-3

**Example:**
```python
session_id = mcp_tool.set_repository_context("/path/to/repo")
callers = mcp_tool.get_call_graph(
    "function_name",
    session_id=session_id,
    direction="callers",
    depth=2
)
```

## Configuration

### Environment Variables

Configure repository isolation behavior:

```bash
# Session timeout in hours (default: 24)
export GRAPHRAG_SESSION_TIMEOUT=24

# Maximum concurrent sessions (default: 10)
export GRAPHRAG_MAX_SESSIONS=10

# Enable verbose logging
export GRAPHRAG_LOG_LEVEL=DEBUG
```

### Collection Cleanup

Automatically clean up old collections:

```python
from auto_coder.graphrag_mcp_integration import GraphRAGMCPIntegration

integration = GraphRAGMCPIntegration()

# Clean up collections older than 30 days
integration.cleanup_collections(max_age_days=30)

# Clean up expired sessions
integration.cleanup_expired_sessions(max_age_hours=24)
```

## Performance Considerations

### Memory Usage
- Each session stores metadata in memory
- Sessions auto-expire to prevent memory leaks
- Monitor session count in production

### Qdrant Collections
- Each repository creates a new collection
- Collections are lightweight
- Monitor collection count for performance

### Neo4j Labels
- Nodes labeled with repository identifiers
- Queries automatically filtered by label
- Minimal performance overhead

## Security

### Data Isolation
- Complete separation between repositories
- No cross-repository data leakage
- Session-based access control

### Access Control
- Sessions tied to specific repositories
- Session IDs are cryptographically random
- Automatic session expiration

## Monitoring

### Session Metrics
Monitor active sessions:

```python
integration = GraphRAGMCPIntegration()

# Get session count
session_count = integration.get_session_count()

# Get session list
sessions = integration.list_sessions()

# Get session details
session_info = integration.get_session_info(session_id)
```

### Collection Metrics
Monitor collection usage:

```python
# Get collection count
collection_count = integration.get_collection_count()

# Get collection list
collections = integration.list_collections()

# Get collection details
collection_info = integration.get_collection_info("repo_abc123")
```

## Best Practices

1. **Always use sessions** when working with multiple repositories
2. **Clean up expired sessions** regularly in production
3. **Monitor session count** to prevent memory issues
4. **Use meaningful session IDs** for debugging
5. **Verify isolation** after migration

## Troubleshooting

### No valid session context
**Cause:** Session ID not provided or expired
**Solution:**
```python
session_id = mcp_tool.set_repository_context("/path/to/repo")
```

### Data contamination still occurring
**Cause:** Using compatibility mode without isolation
**Solution:**
```python
# Use session-based isolation
result = mcp_tool.find_symbol("function", session_id=session_id)
```

### Performance degradation
**Cause:** Large number of collections or sessions
**Solution:**
```python
# Cleanup expired sessions
integration.cleanup_expired_sessions(max_age_hours=24)
```

### Session conflicts
**Cause:** Multiple sessions for same repository
**Solution:**
```python
# Reuse existing session
session_id = integration.get_or_create_session("/path/to/repo")
```

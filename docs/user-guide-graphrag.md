# User Guide: GraphRAG Repository Isolation

## Quick Start (v2.0+)

### 1. Automatic Setup
GraphRAG isolation is enabled by default:

```bash
auto-coder process-issues --issues all
```

The system automatically:
- Creates unique session for your repository
- Sets up repository-specific collections
- Enables complete isolation

### 2. Manual Session Control (Advanced)

For advanced multi-repo workflows:

```python
from auto_coder.graphrag_mcp_integration import GraphRAGMCPIntegration

# Create integration instance
integration = GraphRAGMCPIntegration()

# Create session for specific repository
session_id = integration.create_session("/path/to/your/repo")

# Use session with MCP tools
result = mcp_tool.find_symbol("function_name", session_id=session_id)
```

### 3. Multiple Repository Support

Process multiple repositories simultaneously:

```python
# Repository 1
session1 = integration.create_session("/repo/project-a")
result1 = mcp_tool.semantic_code_search("authentication", session_id=session1)

# Repository 2 (isolated)
session2 = integration.create_session("/repo/project-b")
result2 = mcp_tool.semantic_code_search("authentication", session_id=session2)

# No cross-contamination between repositories
assert result1 != result2
```

## Core Workflows

### Workflow 1: Single Repository Analysis

```bash
# Index repository
auto-coder graphrag index --repo /path/to/repo

# Analyze code
auto-coder graphrag analyze --repo /path/to/repo --query "authentication logic"

# Find symbols
auto-coder graphrag find-symbol --repo /path/to/repo --name "authenticate_user"
```

### Workflow 2: Multi-Repository Comparison

```python
from auto_coder.graphrag_mcp_integration import GraphRAGMCPIntegration

integration = GraphRAGMCPIntegration()

# Setup sessions for each repository
repo_a_session = integration.create_session("/repo/project-a")
repo_b_session = integration.create_session("/repo/project-b")

# Compare authentication patterns
auth_patterns_a = mcp_tool.semantic_code_search(
    "JWT token validation",
    session_id=repo_a_session
)

auth_patterns_b = mcp_tool.semantic_code_search(
    "JWT token validation",
    session_id=repo_b_session
)

# Analyze differences
analyze_differences(auth_patterns_a, auth_patterns_b)
```

### Workflow 3: Batch Processing

```python
repositories = [
    "/repo/service-a",
    "/repo/service-b",
    "/repo/service-c",
]

results = {}

for repo in repositories:
    # Create session
    session_id = integration.create_session(repo)

    # Process repository
    results[repo] = {
        "symbols": mcp_tool.count_symbols(session_id=session_id),
        "classes": mcp_tool.list_classes(session_id=session_id),
        "functions": mcp_tool.list_functions(session_id=session_id),
    }

    # Cleanup session
    integration.cleanup_session(session_id)
```

## Common Tasks

### Task 1: Find a Function Across Repositories

```bash
# Find function in specific repository
auto-coder graphrag find-symbol \
    --repo /path/to/repo \
    --name "process_payment"

# Search across all indexed repositories
auto-coder graphrag search \
    --query "process payment" \
    --all-repos
```

### Task 2: Analyze Code Dependencies

```python
# Get call graph for a function
session_id = integration.create_session("/path/to/repo")
graph = mcp_tool.get_call_graph(
    symbol_id="process_payment",
    session_id=session_id,
    direction="both",
    depth=2
)
```

### Task 3: Search for Patterns

```python
# Semantic search
session_id = integration.create_session("/path/to/repo")
results = mcp_tool.semantic_code_search(
    "error handling retry logic",
    session_id=session_id,
    limit=20
)
```

### Task 4: Generate Documentation

```python
# Generate documentation for repository
session_id = integration.create_session("/path/to/repo")
docs = mcp_tool.generate_documentation(
    module_path="src/auth",
    session_id=session_id
)
```

## CLI Commands Reference

### Indexing Commands

#### `auto-coder graphrag index`
Index repository with repository isolation enabled.

```bash
# Basic indexing
auto-coder graphrag index --repo /path/to/repo

# Force re-index
auto-coder graphrag index --repo /path/to/repo --force

# Index with specific settings
auto-coder graphrag index \
    --repo /path/to/repo \
    --collection-name custom_collection \
    --verbose
```

#### `auto-coder graphrag reindex`
Re-index repository with updated isolation settings.

```bash
auto-coder graphrag reindex --repo /path/to/repo
```

### Query Commands

#### `auto-coder graphrag find-symbol`
Find symbol in repository.

```bash
auto-coder graphrag find-symbol \
    --repo /path/to/repo \
    --name "function_name"

# Find with full name
auto-coder graphrag find-symbol \
    --repo /path/to/repo \
    --name "module.function_name"
```

#### `auto-coder graphrag search`
Semantic code search.

```bash
# Basic search
auto-coder graphrag search \
    --repo /path/to/repo \
    --query "authentication logic"

# Search with limit
auto-coder graphrag search \
    --repo /path/to/repo \
    --query "database connection" \
    --limit 50

# Search all repositories
auto-coder graphrag search \
    --query "authentication logic" \
    --all-repos
```

#### `auto-coder graphrag get-call-graph`
Get call graph for symbol.

```bash
auto-coder graphrag get-call-graph \
    --repo /path/to/repo \
    --symbol "main_function" \
    --direction both \
    --depth 3
```

### Migration Commands

#### `auto-coder graphrag check-migration-status`
Check current migration status.

```bash
auto-coder graphrag check-migration-status
```

#### `auto-coder graphrag migration-preview`
Preview migration changes.

```bash
auto-coder graphrag migration-preview
```

#### `auto-coder graphrag migrate-to-isolation`
Perform migration to repository isolation.

```bash
# Interactive migration
auto-coder graphrag migrate-to-isolation

# Automated migration
auto-coder graphrag migrate-to-isolation --force

# Migration with backup
auto-coder graphrag migrate-to-isolation --backup
```

#### `auto-coder graphrag verify-migration`
Verify migration completed successfully.

```bash
auto-coder graphrag verify-migration
```

### Session Management Commands

#### `auto-coder graphrag session-info`
Show information about active sessions.

```bash
# Show all sessions
auto-coder graphrag session-info

# Show specific session
auto-coder graphrag session-info --session-id abc123
```

#### `auto-coder graphrag cleanup-sessions`
Clean up expired sessions.

```bash
# Cleanup sessions older than 24 hours
auto-coder graphrag cleanup-sessions --max-age 24

# Force cleanup all sessions
auto-coder graphrag cleanup-sessions --force
```

## Configuration

### Configuration File

Create `~/.auto-coder/graphrag.yaml`:

```yaml
repository_isolation:
  enabled: true
  session_timeout: 3600
  max_concurrent_sessions: 100
  collection_prefix: "repo_"

qdrant:
  host: localhost
  port: 6333
  collection_timeout: 30

neo4j:
  host: localhost
  port: 7687
  database: neo4j

logging:
  level: INFO
  sessions: true
```

### Environment Variables

```bash
# Enable repository isolation
export AUTOCODER_GRAPH_ISOLATION=true

# Set session timeout (seconds)
export AUTOCODER_SESSION_TIMEOUT=3600

# Set max concurrent sessions
export AUTOCODER_MAX_SESSIONS=100

# Set Qdrant host
export AUTOCODER_QDRANT_HOST=localhost

# Set Neo4j URI
export AUTOCODER_NEO4J_URI=bolt://localhost:7687
```

## Performance Tips

### 1. Index Optimization
- Index only necessary directories with `--include`
- Exclude test files with `--exclude`
- Use incremental indexing for updates

```bash
# Index only source code
auto-coder graphrag index \
    --repo /path/to/repo \
    --include "src/**/*.py" \
    --exclude "tests/**"
```

### 2. Session Management
- Reuse sessions for multiple operations
- Clean up sessions when done
- Monitor session count

```python
# Create session once
session_id = integration.create_session("/path/to/repo")

# Reuse for multiple operations
for query in queries:
    results = mcp_tool.semantic_code_search(
        query,
        session_id=session_id
    )

# Cleanup at end
integration.cleanup_session(session_id)
```

### 3. Query Optimization
- Use specific queries
- Limit result count
- Cache frequent queries

```python
# Use specific queries
results = mcp_tool.semantic_code_search(
    "validate JWT token",
    session_id=session_id,
    limit=10
)
```

## Troubleshooting

### Issue: "No valid session context"
**Symptoms:** Operations fail with session error
**Solution:**
```python
# Create session
session_id = integration.create_session("/path/to/repo")

# Set context
integration.set_repository_context("/path/to/repo", session_id)
```

### Issue: "Data contamination still occurring"
**Symptoms:** Results from wrong repository
**Solution:**
```python
# Verify session is set
assert session_id is not None

# Use session explicitly
result = mcp_tool.find_symbol("function", session_id=session_id)
```

### Issue: "Performance degradation"
**Symptoms:** Slow queries or high memory usage
**Solution:**
```python
# Cleanup expired sessions
integration.cleanup_expired_sessions(max_age_hours=24)

# Reduce concurrent sessions
integration.max_sessions = 50
```

### Issue: "Migration failed"
**Symptoms:** Migration commands fail
**Solution:**
```bash
# Check detailed status
auto-coder graphrag check-migration-status --verbose

# Manual migration
auto-coder graphrag migrate-to-isolation --verbose --force

# Restore from backup
auto-coder graphrag restore-from-backup <backup-id>
```

## Examples

### Example 1: Security Audit
```python
# Setup
integration = GraphRAGMCPIntegration()

# Find all authentication-related functions
session_id = integration.create_session("/repo/your-app")
auth_functions = mcp_tool.semantic_code_search(
    "authentication authorization login",
    session_id=session_id
)

# Check for security issues
for func in auth_functions:
    if "eval" in func.code or "exec" in func.code:
        print(f"Security issue in {func.name}")
```

### Example 2: Refactoring Analysis
```python
# Find deprecated API usage
session_id = integration.create_session("/repo/your-app")
deprecated_usage = mcp_tool.semantic_code_search(
    "old_api deprecated",
    session_id=session_id
)

# Generate refactoring plan
plan = generate_refactoring_plan(deprecated_usage)
print(plan)
```

### Example 3: Code Review
```python
# Setup session
session_id = integration.create_session("/repo/your-app")

# Find complex functions
complex_functions = mcp_tool.list_functions(
    complexity="high",
    session_id=session_id
)

# Generate review comments
for func in complex_functions:
    comment = generate_review_comment(func)
    print(f"{func.file}:{func.line}: {comment}")
```

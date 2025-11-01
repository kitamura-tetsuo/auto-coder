# GraphRAG Repository Isolation Migration Guide

## What's Changed?

### Before (v1.x)
- All repositories shared single Qdrant collection and Neo4j graph
- Data contamination possible when processing multiple repositories
- Limited scalability for multi-repo workflows

### After (v2.0+)
- Repository-specific Qdrant collections for complete isolation
- Session-based context management for multi-repo workflows
- Neo4j repository labels for query isolation
- Backward compatibility maintained

## Benefits

1. **Data Isolation**: Each repository's code analysis is completely isolated
2. **Accurate Results**: Semantic search returns only relevant repository symbols
3. **Multi-Repo Support**: Process multiple repositories simultaneously without interference
4. **Performance**: Optimized queries with repository-specific filtering
5. **Scalability**: Better resource utilization for large codebases

## Migration Process

### Step 1: Automatic Detection
The system automatically detects your current setup:

```bash
auto-coder graphrag check-migration-status
```

### Step 2: Review Impact
Check what will be migrated:

```bash
auto-coder graphrag migration-preview
```

### Step 3: Perform Migration
Migrate to repository isolation:

```bash
auto-coder graphrag migrate-to-isolation
```

### Step 4: Verify Results
Verify migration success:

```bash
auto-coder graphrag verify-migration
```

### Step 5: Update Scripts (if needed)
Update any custom scripts using GraphRAG MCP.

## Migration Code Examples

### Before (v1.x)
```python
# All repositories shared global context
result = mcp_tool.find_symbol("function_name")
results = mcp_tool.semantic_code_search("user authentication")
```

### After (v2.0+)
```python
# Session-based repository isolation
session_id = mcp_tool.set_repository_context("/path/to/repo")

# Repository-specific operations
result = mcp_tool.find_symbol("function_name", session_id=session_id)
results = mcp_tool.semantic_code_search("user authentication", session_id=session_id)
```

### Backward Compatibility (2.0+)
```python
# Legacy code still works (with warnings)
result = mcp_tool.find_symbol("function_name")  # Works but shows warning
```

### Migration Helper
```python
# Auto-coder handles sessions internally
client = CodexMCPClient()
result = client.analyze_function("test")  # Automatic session management
```

## Common Migration Scenarios

### Scenario 1: Single Repository User
If you're only working with one repository, the migration is seamless:

```bash
# Old workflow - still works
auto-coder process-issues --issues all

# New workflow - automatic session management
auto-coder process-issues --issues all
# Session automatically created for current repository
```

### Scenario 2: Multiple Repository User
If you're working with multiple repositories, you need session management:

```python
# Repository 1
session1 = mcp_tool.set_repository_context("/path/to/repo1")
result1 = mcp_tool.semantic_code_search("auth", session_id=session1)

# Repository 2
session2 = mcp_tool.set_repository_context("/path/to/repo2")
result2 = mcp_tool.semantic_code_search("auth", session_id=session2)
```

### Scenario 3: Automated Workflows
For CI/CD or automated scripts:

```python
from auto_coder.graphrag_mcp_integration import GraphRAGMCPIntegration

integration = GraphRAGMCPIntegration()

# Process each repository with isolation
for repo_path in ["/path/to/repo1", "/path/to/repo2"]:
    session_id = integration.create_session(repo_path)
    result = integration.analyze_repository(session_id)
```

## Post-Migration Checklist

- [ ] Verify all repositories can be queried independently
- [ ] Check that no cross-contamination occurs between repositories
- [ ] Update any custom scripts to use session_id parameter
- [ ] Monitor performance for any degradation
- [ ] Clean up old shared collections (optional)

## Troubleshooting

### Issue: "Session context not found"
**Solution**: Ensure you call `set_repository_context()` before using MCP tools

### Issue: "Data contamination still occurring"
**Solution**: Verify you're using session_id in all queries

### Issue: "Performance degradation"
**Solution**: Run cleanup to remove expired sessions and unused collections

### Issue: "Migration failed"
**Solution**: Check logs and run with --verbose flag for detailed output

## Rollback Procedure

If you need to rollback to v1.x (not recommended):

1. Contact support for rollback script
2. Data from v2.0+ can be migrated back to shared collection
3. Some isolation benefits will be lost

## Additional Resources

- [GraphRAG Repository Isolation Architecture](graphrag-repository-isolation.md)
- [User Guide](user-guide-graphrag.md)
- [Developer Documentation](developer-graphrag-isolation.md)

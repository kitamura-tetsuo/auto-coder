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

## Troubleshooting

### Issue: "No valid session context"
**Cause:** Session ID not provided or expired
**Solution:**
```python
session_id = mcp_tool.set_repository_context("/path/to/repo")
```

### Issue: "Data contamination still occurring"
**Cause:** Using compatibility mode without isolation
**Solution:**
```python
# Use session-based isolation
result = mcp_tool.find_symbol("function", session_id=session_id)
```

### Issue: Performance degradation
**Cause:** Large number of collections or sessions
**Solution:**
```python
# Cleanup expired sessions
integration.cleanup_expired_sessions(max_age_hours=24)
```

### Issue: Migration failed
**Cause:** Insufficient permissions or disk space
**Solution:**
```bash
# Check migration status
auto-coder graphrag migration-status

# Manual migration with verbose output
auto-coder graphrag migrate-to-isolation --verbose
```

## Rollback Procedure

If you need to rollback the migration:

1. **Create backup before migration** (automatically done with --backup flag)
2. **Restore previous state**:
   ```bash
   auto-coder graphrag restore-from-backup <backup-id>
   ```
3. **Verify rollback**:
   ```bash
   auto-coder graphrag verify-rollback
   ```

## Frequently Asked Questions

### Q: Will my existing indexes be deleted?
**A:** No, the migration process creates backups of all existing data before making changes.

### Q: Do I need to update my existing scripts?
**A:** No, backward compatibility is maintained. Legacy code will continue to work with a deprecation warning.

### Q: How long does migration take?
**A:** Typically 5-15 minutes depending on repository size and index complexity.

### Q: Can I migrate multiple repositories?
**A:** Yes, the migration process handles multiple repositories automatically.

### Q: What if I encounter errors during migration?
**A:** Check the troubleshooting section above or run migration with --verbose flag for detailed diagnostics.

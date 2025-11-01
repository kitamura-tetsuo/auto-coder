# User Guide: GraphRAG Repository Isolation

## Table of Contents
1. [Getting Started](#getting-started)
2. [Quick Start](#quick-start)
3. [Manual Session Control](#manual-session-control)
4. [Multiple Repository Support](#multiple-repository-support)
5. [Common Use Cases](#common-use-cases)
6. [CLI Commands](#cli-commands)
7. [Troubleshooting](#troubleshooting)

## Getting Started

### What is Repository Isolation?

Repository isolation ensures that when you're working with multiple code repositories, each repository's code analysis data is completely separate. This means:

- Semantic search results only show symbols from the current repository
- Call graphs are scoped to the current repository
- No data contamination between different repositories

### Why Use Repository Isolation?

If you're working with multiple repositories (e.g., a monorepo with multiple projects, or multiple client projects), repository isolation provides:

1. **Accurate Results**: Search results are relevant to the repository you're working on
2. **Better Performance**: Smaller, focused datasets
3. **No Confusion**: Clear separation between projects
4. **Scalability**: Can handle many repositories efficiently

## Quick Start

### Automatic Setup (v2.0+)

Repository isolation is enabled by default in v2.0+:

```bash
auto-coder process-issues --issues all
```

The system automatically:
- Creates a unique session for your repository
- Sets up repository-specific collections
- Enables complete isolation

That's it! No additional configuration needed for simple use cases.

### First Time Setup

If you're new to GraphRAG:

1. **Start GraphRAG services**:
   ```bash
   auto-coder graphrag start
   ```

2. **Process your repository**:
   ```bash
   auto-coder process-issues --issues all --repo-path /path/to/your/repo
   ```

3. **Use GraphRAG features**:
   ```bash
   # The session is automatically created for your repository
   auto-coder query --find-symbol "my_function"
   ```

## Manual Session Control (Advanced)

For advanced multi-repository workflows, you can manually control sessions.

### Setting Repository Context

```python
from auto_coder.graphrag_mcp_integration import GraphRAGMCPIntegration

# Create integration instance
integration = GraphRAGMCPIntegration()

# Create session for specific repository
session_id = integration.create_session("/path/to/your/repo")

# Use session with MCP tools
result = mcp_tool.find_symbol("function_name", session_id=session_id)
```

### Managing Sessions

```python
# Get existing session or create new one
session_id = integration.get_or_create_session("/path/to/repo")

# Check session status
session_info = integration.get_session_info(session_id)
print(f"Session: {session_info['repository']}")
print(f"Created: {session_info['created_at']}")

# List all active sessions
sessions = integration.list_sessions()
for session in sessions:
    print(f"  {session['session_id']}: {session['repository_path']}")
```

### Session Cleanup

```python
# Clean up expired sessions (older than 24 hours)
integration.cleanup_expired_sessions(max_age_hours=24)

# Clean up all sessions for a specific repository
integration.cleanup_session("/path/to/repo")

# Clean up all sessions
integration.cleanup_all_sessions()
```

## Multiple Repository Support

### Working with Multiple Repositories

Process multiple repositories simultaneously with complete isolation:

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

### Switching Between Repositories

```python
# Switch to Repository A
session_a = integration.create_session("/repo/project-a")
mcp_tool.set_current_session(session_a)
result_a = mcp_tool.find_symbol("utils")

# Switch to Repository B
session_b = integration.create_session("/repo/project-b")
mcp_tool.set_current_session(session_b)
result_b = mcp_tool.find_symbol("utils")
```

### Best Practices for Multi-Repo Workflows

1. **Use descriptive session names**:
   ```python
   session_id = integration.create_session(
       "/repo/project-a",
       session_name="project-a-feature-branch"
   )
   ```

2. **Track session ownership**:
   ```python
   sessions = {
       "project-a": integration.create_session("/repo/project-a"),
       "project-b": integration.create_session("/repo/project-b"),
   }
   ```

3. **Clean up sessions** when done:
   ```python
   for session_id in sessions.values():
       integration.cleanup_session(session_id)
   ```

## Common Use Cases

### Use Case 1: Single Developer, Multiple Projects

You're a developer working on multiple client projects:

```python
# Morning: Work on Project A
session_a = integration.create_session("/home/user/clients/project-a")
result = mcp_tool.semantic_code_search("payment processing", session_id=session_a)

# Afternoon: Work on Project B
session_b = integration.create_session("/home/user/clients/project-b")
result = mcp_tool.semantic_code_search("payment processing", session_id=session_b)
# Results show Project B code only, not Project A
```

### Use Case 2: Monorepo with Multiple Packages

You're working in a monorepo with multiple packages:

```python
# Work on UI package
session_ui = integration.create_session("/monorepo/packages/ui")
components = mcp_tool.find_symbol("Button", session_id=session_ui)

# Work on API package
session_api = integration.create_session("/monorepo/packages/api")
endpoints = mcp_tool.find_symbol("UserController", session_id=session_api)
```

### Use Case 3: CI/CD Pipeline

Automated builds across multiple repositories:

```python
# Build script for all repositories
repositories = [
    "/build/repo-1",
    "/build/repo-2",
    "/build/repo-3"
]

for repo_path in repositories:
    session_id = integration.create_session(repo_path)
    result = integration.analyze_repository(session_id)

    if not result["success"]:
        print(f"Failed to analyze {repo_path}")
        # Handle error

    # Clean up session after analysis
    integration.cleanup_session(session_id)
```

### Use Case 4: Code Review Across Repositories

Reviewing code changes that span multiple repositories:

```python
# Set up context for review
main_session = integration.create_session("/repo/main")
feature_session = integration.create_session("/repo/feature-branch")

# Compare implementations
main_symbols = mcp_tool.semantic_code_search("user validation", session_id=main_session)
feature_symbols = mcp_tool.semantic_code_search("user validation", session_id=feature_session)

# Identify differences
diff = set(feature_symbols) - set(main_symbols)
```

## CLI Commands

### Basic Commands

```bash
# Start GraphRAG services
auto-coder graphrag start

# Stop GraphRAG services
auto-coder graphrag stop

# Check status
auto-coder graphrag status

# Update index for current repository
auto-coder graphrag update-index
```

### Migration Commands

```bash
# Check migration status
auto-coder graphrag check-migration-status

# Preview migration
auto-coder graphrag migration-preview

# Perform migration
auto-coder graphrag migrate-to-isolation

# Verify migration
auto-coder graphrag verify-migration

# List active sessions
auto-coder graphrag session-info

# Get session details
auto-coder graphrag session-info --session-id abc123
```

### Index Management

```bash
# Force update index
auto-coder graphrag update-index --force

# Update index for specific repository
auto-coder graphrag update-index --repo-path /path/to/repo

# Setup GraphRAG MCP
auto-coder graphrag setup-mcp
```

## Troubleshooting

### Issue: "No valid session context"

**Cause:** Session ID not provided or expired

**Solution:**
```python
# Create a new session
session_id = mcp_tool.set_repository_context("/path/to/repo")

# Or get existing session
session_id = integration.get_or_create_session("/path/to/repo")
```

### Issue: "Data contamination still occurring"

**Cause:** Using compatibility mode without isolation

**Solution:**
```python
# Use session-based isolation
result = mcp_tool.find_symbol("function", session_id=session_id)

# Verify session is set correctly
session_info = integration.get_session_info(session_id)
print(f"Current repository: {session_info['repository_path']}")
```

### Issue: "Performance degradation"

**Cause:** Large number of collections or sessions

**Solution:**
```python
# Cleanup expired sessions
integration.cleanup_expired_sessions(max_age_hours=24)

# Monitor session count
session_count = integration.get_session_count()
if session_count > 50:
    print("Warning: Many active sessions")
```

### Issue: "Session not found"

**Cause:** Session expired or ID is incorrect

**Solution:**
```python
# Create new session
session_id = integration.create_session("/path/to/repo")

# Check active sessions
sessions = integration.list_sessions()
print("Active sessions:")
for session in sessions:
    print(f"  {session['session_id']}: {session['repository_path']}")
```

### Issue: "Migration failed"

**Cause:** Insufficient permissions or disk space

**Solution:**
```bash
# Check migration status
auto-coder graphrag check-migration-status

# Run migration with verbose output
auto-coder graphrag migrate-to-isolation --verbose

# Check disk space
df -h

# Check permissions
ls -la /path/to/data
```

## Advanced Tips

### Tip 1: Reuse Sessions

Avoid creating new sessions for the same repository:

```python
# Good: Reuse existing session
session_id = integration.get_or_create_session("/path/to/repo")

# Avoid: Creating multiple sessions for same repo
# sessions = [
#     integration.create_session("/path/to/repo"),
#     integration.create_session("/path/to/repo"),  # Don't do this
# ]
```

### Tip 2: Use Session Naming

Name your sessions for easier debugging:

```python
session_id = integration.create_session(
    "/path/to/repo",
    session_name="feature-payment-refactor"
)
```

### Tip 3: Monitor Resources

Keep track of resources in production:

```python
# Monitor collection count
collection_count = integration.get_collection_count()
print(f"Total collections: {collection_count}")

# Monitor session count
session_count = integration.get_session_count()
print(f"Active sessions: {session_count}")
```

### Tip 4: Batch Operations

Process multiple repositories efficiently:

```python
# Batch create sessions
repo_paths = ["/repo1", "/repo2", "/repo3"]
sessions = {}
for repo_path in repo_paths:
    sessions[repo_path] = integration.create_session(repo_path)

# Batch process
for repo_path, session_id in sessions.items():
    result = mcp_tool.semantic_code_search("TODO", session_id=session_id)
    print(f"{repo_path}: {len(result)} TODOs found")

# Batch cleanup
for session_id in sessions.values():
    integration.cleanup_session(session_id)
```

## Frequently Asked Questions

**Q: Do I need to update my existing scripts?**

A: No, backward compatibility is maintained. Legacy scripts will work but may show warnings. For best results, add session_id parameter.

**Q: How long do sessions last?**

A: Sessions expire after 24 hours by default. You can customize this with `GRAPHRAG_SESSION_TIMEOUT` environment variable.

**Q: Can I have multiple sessions for the same repository?**

A: Yes, but it's not recommended. Use `get_or_create_session()` to avoid duplicates.

**Q: What happens to my old data?**

A: Old data is migrated to the new repository-specific collections. No data is lost.

**Q: How do I switch between repositories in the same script?**

A: Create separate sessions for each repository and use the session_id parameter in your MCP tool calls.

**Q: Is there a limit to how many repositories I can work with?**

A: The default limit is 10 concurrent sessions. You can increase this with `GRAPHRAG_MAX_SESSIONS` environment variable.

## Additional Resources

- [Migration Guide](graphrag-migration-guide.md) - Detailed migration steps
- [Architecture Documentation](graphrag-repository-isolation.md) - Technical deep dive
- [Developer Guide](developer-graphrag-isolation.md) - For developers

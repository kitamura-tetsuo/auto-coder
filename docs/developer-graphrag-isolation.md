# Developer Guide: GraphRAG Repository Isolation

## Table of Contents
1. [Architecture Overview](#architecture-overview)
2. [Implementation Details](#implementation-details)
3. [API Reference](#api-reference)
4. [Testing](#testing)
5. [Extending the System](#extending-the-system)
6. [Best Practices](#best-practices)
7. [Performance Optimization](#performance-optimization)
8. [Troubleshooting](#troubleshooting)

## Architecture Overview

### System Components

The repository isolation system consists of several key components:

```
┌─────────────────────────────────────────────────────────────┐
│                     GraphRAGMCPIntegration                   │
├─────────────────────────────────────────────────────────────┤
│  ┌────────────────┐  ┌──────────────────┐  ┌──────────────┐ │
│  │ SessionManager │  │ CollectionManager│  │ QueryManager │ │
│  └────────────────┘  └──────────────────┘  └──────────────┘ │
└─────────────────────────────────────────────────────────────┘
           │                      │                      │
           ▼                      ▼                      ▼
┌────────────────┐  ┌──────────────────┐  ┌──────────────┐
│ Session Store  │  │ Qdrant/Neo4j     │  │ MCP Tools    │
│ (In-Memory)    │  │ Repositories     │  │ (Enhanced)   │
└────────────────┘  └──────────────────┘  └──────────────┘
```

### Core Classes

#### GraphRAGMCPIntegration
Main integration class that orchestrates all repository isolation functionality.

```python
class GraphRAGMCPIntegration:
    """Main integration class for GraphRAG repository isolation."""

    def __init__(self, ...):
        """Initialize with Docker manager, index manager, and session management."""
        self.docker_manager = docker_manager
        self.index_manager = index_manager
        self.session_manager = SessionManager()
        self.collection_manager = CollectionManager()
```

#### SessionManager
Manages session lifecycle and repository context.

```python
class SessionManager:
    """Manages repository isolation sessions."""

    def create_session(self, repo_path: str, session_name: str = None) -> str:
        """Create a new session for a repository."""

    def get_session(self, session_id: str) -> Session:
        """Retrieve session by ID."""

    def cleanup_session(self, session_id: str) -> bool:
        """Clean up a specific session."""

    def cleanup_expired_sessions(self, max_age_hours: int = 24) -> int:
        """Clean up all expired sessions."""
```

#### CollectionManager
Manages Qdrant collections and Neo4j labels.

```python
class CollectionManager:
    """Manages repository-specific collections."""

    def create_collection(self, repo_hash: str, repo_path: str) -> str:
        """Create a new collection for a repository."""

    def get_collection_name(self, repo_path: str) -> str:
        """Get collection name for a repository."""

    def cleanup_collections(self, max_age_days: int = 30) -> int:
        """Clean up old collections."""
```

## Implementation Details

### Session Creation Flow

```python
def create_session(self, repo_path: str, session_name: str = None) -> str:
    """Create a new session with repository isolation."""

    # 1. Validate repository path
    if not os.path.exists(repo_path):
        raise ValueError(f"Repository path does not exist: {repo_path}")

    # 2. Generate repository hash
    repo_hash = self._generate_repo_hash(repo_path)

    # 3. Create session ID
    session_id = self._generate_session_id()

    # 4. Create or verify collection
    collection_name = self.collection_manager.create_collection(repo_hash, repo_path)

    # 5. Create session object
    session = Session(
        session_id=session_id,
        repository_path=repo_path,
        repository_hash=repo_hash,
        collection_name=collection_name,
        created_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(hours=self.session_timeout),
        session_name=session_name
    )

    # 6. Store session
    self.session_manager.store_session(session)

    # 7. Return session ID
    return session_id
```

### Hash Generation

Repository hash is used to create unique collection names:

```python
def _generate_repo_hash(self, repo_path: str) -> str:
    """Generate SHA-256 hash of repository path."""

    # Use absolute path to ensure consistency
    abs_path = os.path.abspath(repo_path)

    # Create hash
    hash_obj = hashlib.sha256(abs_path.encode('utf-8'))
    hash_hex = hash_obj.hexdigest()

    return hash_hex

# Example:
# /home/user/projects/my-app -> abc123def456...
# /workspace/client-a -> 789ghi012jkl...
```

### Collection Naming

Collections are named using the hash:

```python
def create_collection(self, repo_hash: str, repo_path: str) -> str:
    """Create collection with standardized naming."""

    collection_name = f"repo_{repo_hash}"

    # Verify collection doesn't already exist
    if self.qdrant_client.collection_exists(collection_name):
        return collection_name

    # Create collection with appropriate configuration
    self.qdrant_client.create_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(size=384, distance=Distance.COSINE)
    )

    return collection_name
```

### Query Isolation

All queries automatically filter by repository:

```python
def find_symbol(self, fqname: str, session_id: str = None, **kwargs):
    """Find symbol with repository isolation."""

    # Use session if provided
    if session_id:
        session = self.session_manager.get_session(session_id)
        collection_name = session.collection_name
    else:
        # Backward compatibility: use default collection
        collection_name = self.default_collection

    # Perform query with collection filter
    results = self.qdrant_client.search(
        collection_name=collection_name,
        query_vector=self.encode_symbol(fqname),
        **kwargs
    )

    return results
```

## API Reference

### Core Methods

#### create_session()

Create a new repository isolation session.

```python
def create_session(
    self,
    repo_path: str,
    session_name: str = None,
    timeout_hours: int = None
) -> str:
    """Create a new session for repository isolation.

    Args:
        repo_path: Absolute path to repository
        session_name: Optional human-readable name
        timeout_hours: Session timeout in hours (default: 24)

    Returns:
        session_id: Unique session identifier

    Raises:
        ValueError: If repository path doesn't exist
        RuntimeError: If unable to create collection

    Example:
        session_id = integration.create_session(
            "/path/to/repo",
            session_name="feature-branch"
        )
    """
```

#### get_or_create_session()

Get existing session or create new one.

```python
def get_or_create_session(self, repo_path: str) -> str:
    """Get existing session for repository or create new one.

    This is the recommended way to work with sessions to avoid
    creating duplicates.

    Args:
        repo_path: Absolute path to repository

    Returns:
        session_id: Session identifier

    Example:
        session_id = integration.get_or_create_session("/path/to/repo")
    """
```

#### cleanup_session()

Clean up a specific session.

```python
def cleanup_session(self, session_id: str) -> bool:
    """Clean up a specific session.

    Args:
        session_id: Session to clean up

    Returns:
        bool: True if successful, False otherwise

    Example:
        success = integration.cleanup_session("abc123")
    """
```

#### cleanup_expired_sessions()

Clean up all expired sessions.

```python
def cleanup_expired_sessions(self, max_age_hours: int = 24) -> int:
    """Clean up all expired sessions.

    Args:
        max_age_hours: Maximum age in hours before session expires

    Returns:
        int: Number of sessions cleaned up

    Example:
        cleaned = integration.cleanup_expired_sessions(max_age_hours=48)
        print(f"Cleaned up {cleaned} expired sessions")
    """
```

### Utility Methods

#### get_session_info()

Get detailed session information.

```python
def get_session_info(self, session_id: str) -> dict:
    """Get session information.

    Args:
        session_id: Session identifier

    Returns:
        dict: Session information

    Example:
        info = integration.get_session_info("abc123")
        print(f"Repository: {info['repository_path']}")
        print(f"Created: {info['created_at']}")
        print(f"Expires: {info['expires_at']}")
    """
```

#### list_sessions()

List all active sessions.

```python
def list_sessions(self) -> List[dict]:
    """List all active sessions.

    Returns:
        List[dict]: List of session information

    Example:
        sessions = integration.list_sessions()
        for session in sessions:
            print(f"{session['session_id']}: {session['repository_path']}")
    """
```

#### get_session_count()

Get count of active sessions.

```python
def get_session_count(self) -> int:
    """Get number of active sessions.

    Returns:
        int: Number of active sessions

    Example:
        count = integration.get_session_count()
        if count > 50:
            print("Warning: Many active sessions")
    """
```

## Testing

### Unit Tests

Test session management:

```python
import pytest
from auto_coder.graphrag_mcp_integration import GraphRAGMCPIntegration

class TestSessionManager:
    """Test session management functionality."""

    def test_create_session(self, integration, tmp_path):
        """Test session creation."""
        # Arrange
        repo_path = tmp_path / "test_repo"
        repo_path.mkdir()

        # Act
        session_id = integration.create_session(str(repo_path))

        # Assert
        assert session_id is not None
        assert len(session_id) > 0

        # Verify session stored
        session = integration.session_manager.get_session(session_id)
        assert session is not None
        assert session.repository_path == str(repo_path)

    def test_get_or_create_session(self, integration, tmp_path):
        """Test get or create session."""
        # Arrange
        repo_path = tmp_path / "test_repo"
        repo_path.mkdir()

        # Act
        session_id1 = integration.get_or_create_session(str(repo_path))
        session_id2 = integration.get_or_create_session(str(repo_path))

        # Assert
        assert session_id1 == session_id2  # Should return same session

    def test_cleanup_session(self, integration, tmp_path):
        """Test session cleanup."""
        # Arrange
        repo_path = tmp_path / "test_repo"
        repo_path.mkdir()
        session_id = integration.create_session(str(repo_path))

        # Act
        success = integration.cleanup_session(session_id)

        # Assert
        assert success is True
        assert integration.session_manager.get_session(session_id) is None
```

### Integration Tests

Test repository isolation:

```python
class TestRepositoryIsolation:
    """Test repository isolation functionality."""

    def test_isolated_search(self, integration, tmp_path):
        """Test that search results are isolated per repository."""
        # Arrange
        repo1_path = tmp_path / "repo1"
        repo2_path = tmp_path / "repo2"
        repo1_path.mkdir()
        repo2_path.mkdir()

        # Create files with same function name
        (repo1_path / "file1.py").write_text("def calculate(): pass")
        (repo2_path / "file2.py").write_text("def calculate(): pass")

        # Create sessions
        session1 = integration.create_session(str(repo1_path))
        session2 = integration.create_session(str(repo2_path))

        # Act
        results1 = integration.mcp_tool.find_symbol("calculate", session_id=session1)
        results2 = integration.mcp_tool.find_symbol("calculate", session_id=session2)

        # Assert
        assert len(results1) == 1
        assert len(results2) == 1
        assert results1[0].metadata["repo_path"] == str(repo1_path)
        assert results2[0].metadata["repo_path"] == str(repo2_path)

    def test_no_cross_contamination(self, integration, tmp_path):
        """Test that data doesn't cross-contaminate between repositories."""
        # Arrange
        repo1_path = tmp_path / "repo1"
        repo2_path = tmp_path / "repo2"
        repo1_path.mkdir()
        repo2_path.mkdir()

        # Add unique content to each repo
        (repo1_path / "unique1.py").write_text("class ProjectAClass")
        (repo2_path / "unique2.py").write_text("class ProjectBClass")

        # Create sessions
        session1 = integration.create_session(str(repo1_path))
        session2 = integration.create_session(str(repo2_path))

        # Act - search for each other's unique content
        results1 = integration.mcp_tool.semantic_code_search("ProjectBClass", session_id=session1)
        results2 = integration.mcp_tool.semantic_code_search("ProjectAClass", session_id=session2)

        # Assert - should find nothing
        assert len(results1) == 0
        assert len(results2) == 0
```

### Performance Tests

Test session performance:

```python
class TestSessionPerformance:
    """Test session performance."""

    def test_session_creation_speed(self, integration, tmp_path):
        """Test session creation speed."""
        # Arrange
        repo_path = tmp_path / "test_repo"
        repo_path.mkdir()

        # Act
        start_time = time.time()
        session_id = integration.create_session(str(repo_path))
        end_time = time.time()

        # Assert
        creation_time = end_time - start_time
        assert creation_time < 1.0  # Should create in less than 1 second

    def test_many_sessions(self, integration, tmp_path):
        """Test handling many concurrent sessions."""
        # Arrange
        repo_paths = []
        for i in range(50):
            repo_path = tmp_path / f"repo_{i}"
            repo_path.mkdir()
            repo_paths.append(str(repo_path))

        # Act
        start_time = time.time()
        sessions = []
        for repo_path in repo_paths:
            session_id = integration.create_session(repo_path)
            sessions.append(session_id)
        end_time = time.time()

        # Assert
        total_time = end_time - start_time
        assert total_time < 30.0  # Should handle 50 sessions in 30 seconds
        assert len(sessions) == 50

        # Cleanup
        for session_id in sessions:
            integration.cleanup_session(session_id)
```

## Extending the System

### Adding Custom Session Storage

Implement custom session storage:

```python
class CustomSessionStore(SessionStore):
    """Custom session storage implementation."""

    def __init__(self, storage_backend: str = "redis"):
        self.storage_backend = storage_backend
        if storage_backend == "redis":
            import redis
            self.redis_client = redis.Redis(host='localhost', port=6379)

    def store_session(self, session: Session) -> None:
        """Store session in custom backend."""
        if self.storage_backend == "redis":
            self.redis_client.setex(
                f"session:{session.session_id}",
                session.expires_at - datetime.utcnow(),
                json.dumps(session.to_dict())
            )

    def get_session(self, session_id: str) -> Optional[Session]:
        """Retrieve session from custom backend."""
        if self.storage_backend == "redis":
            data = self.redis_client.get(f"session:{session_id}")
            if data:
                return Session.from_dict(json.loads(data))
        return None
```

### Custom Collection Naming

Implement custom collection naming strategy:

```python
class CustomCollectionManager(CollectionManager):
    """Custom collection naming strategy."""

    def create_collection(self, repo_hash: str, repo_path: str) -> str:
        """Create collection with custom naming."""
        # Use repository name instead of hash
        repo_name = os.path.basename(repo_path)
        collection_name = f"{repo_name}_{repo_hash[:8]}"

        # Verify collection doesn't exist
        if self.qdrant_client.collection_exists(collection_name):
            return collection_name

        # Create with custom configuration
        self.qdrant_client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=384, distance=Distance.COSINE)
        )

        return collection_name
```

### Custom Query Filtering

Add custom query filters:

```python
class CustomQueryManager(QueryManager):
    """Custom query filtering."""

    def semantic_search(
        self,
        query: str,
        session_id: str = None,
        filters: dict = None,
        **kwargs
    ):
        """Perform semantic search with custom filters."""
        # Get base filters from session
        base_filters = {}
        if session_id:
            session = self.session_manager.get_session(session_id)
            base_filters["repository_hash"] = session.repository_hash

        # Merge with custom filters
        if filters:
            base_filters.update(filters)

        # Perform search
        results = self.qdrant_client.search(
            collection_name=session.collection_name if session_id else self.default_collection,
            query_vector=self.encode_query(query),
            query_filter=base_filters,
            **kwargs
        )

        return results
```

## Best Practices

### Session Management

1. **Always use get_or_create_session()**:
   ```python
   # Good
   session_id = integration.get_or_create_session("/path/to/repo")

   # Avoid
   session_id = integration.create_session("/path/to/repo")
   ```

2. **Clean up sessions when done**:
   ```python
   try:
       session_id = integration.get_or_create_session("/path/to/repo")
       # Do work
   finally:
       integration.cleanup_session(session_id)
   ```

3. **Use meaningful session names**:
   ```python
   session_id = integration.create_session(
       "/path/to/repo",
       session_name="payment-feature-debug"
   )
   ```

### Error Handling

Always handle exceptions:

```python
try:
    session_id = integration.create_session("/path/to/repo")
except ValueError as e:
    print(f"Invalid repository path: {e}")
except RuntimeError as e:
    print(f"Failed to create session: {e}")
except Exception as e:
    print(f"Unexpected error: {e}")
```

### Configuration

Use environment variables for configuration:

```python
import os

# Session timeout
SESSION_TIMEOUT = int(os.environ.get("GRAPHRAG_SESSION_TIMEOUT", 24))

# Max sessions
MAX_SESSIONS = int(os.environ.get("GRAPHRAG_MAX_SESSIONS", 10))

# Collection cleanup
CLEANUP_INTERVAL = int(os.environ.get("GRAPHRAG_CLEANUP_INTERVAL", 3600))  # seconds
```

## Performance Optimization

### Session Pooling

Reuse sessions to avoid overhead:

```python
class SessionPool:
    """Pool of reusable sessions."""

    def __init__(self, integration: GraphRAGMCPIntegration):
        self.integration = integration
        self.pool = {}
        self.max_size = 10

    def get_session(self, repo_path: str) -> str:
        """Get session from pool or create new one."""
        if repo_path in self.pool:
            return self.pool[repo_path]

        if len(self.pool) >= self.max_size:
            # Remove oldest session
            oldest = next(iter(self.pool))
            del self.pool[oldest]

        session_id = self.integration.get_or_create_session(repo_path)
        self.pool[repo_path] = session_id
        return session_id
```

### Lazy Collection Creation

Create collections only when needed:

```python
class LazyCollectionManager(CollectionManager):
    """Lazy collection creation."""

    def get_collection_name(self, repo_path: str) -> str:
        """Get collection name without creating it."""
        repo_hash = self._generate_repo_hash(repo_path)
        return f"repo_{repo_hash}"

    def ensure_collection(self, repo_path: str) -> str:
        """Ensure collection exists."""
        collection_name = self.get_collection_name(repo_path)

        if not self.qdrant_client.collection_exists(collection_name):
            self.create_collection_from_path(repo_path)

        return collection_name
```

### Query Caching

Cache query results:

```python
from functools import lru_cache
import hashlib

class CachedQueryManager:
    """Query manager with caching."""

    def __init__(self, base_manager: QueryManager):
        self.base_manager = base_manager

    @lru_cache(maxsize=100)
    def find_symbol(self, fqname: str, session_id: str, cache_key: str = None):
        """Find symbol with caching."""
        return self.base_manager.find_symbol(fqname, session_id)

    def clear_cache(self):
        """Clear query cache."""
        self.find_symbol.cache_clear()
```

## Troubleshooting

### Common Issues

#### Issue: Session Creation Fails

**Symptoms:**
```
RuntimeError: Failed to create collection
```

**Causes:**
1. Qdrant not running
2. Insufficient permissions
3. Collection name conflict

**Solutions:**
```python
# Check Qdrant is running
integration.docker_manager.ensure_running()

# Check permissions
assert os.access("/path/to/qdrant", os.W_OK)

# Use unique collection name
collection_name = f"repo_{repo_hash}_{int(time.time())}"
```

#### Issue: Memory Leak

**Symptoms:**
- Memory usage grows over time
- Sessions not being cleaned up

**Solutions:**
```python
# Enable automatic cleanup
integration.session_manager.start_auto_cleanup(interval=3600)  # 1 hour

# Manual cleanup
integration.cleanup_expired_sessions(max_age_hours=1)

# Monitor session count
session_count = integration.get_session_count()
if session_count > 100:
    print("Warning: Many sessions")
```

#### Issue: Slow Query Performance

**Symptoms:**
- Queries taking longer than expected
- High CPU usage

**Solutions:**
```python
# Check collection count
collection_count = integration.get_collection_count()
if collection_count > 100:
    print("Consider cleaning up old collections")

# Use appropriate limits
results = mcp_tool.semantic_code_search(
    "query",
    session_id=session_id,
    limit=10  # Don't return too many results
)

# Enable query caching
integration.query_manager.enable_cache(maxsize=100)
```

### Debugging

Enable debug logging:

```python
import logging
logging.basicConfig(level=logging.DEBUG)

# Or set environment variable
os.environ["GRAPHRAG_LOG_LEVEL"] = "DEBUG"
```

Check session state:

```python
# List all sessions
sessions = integration.list_sessions()
for session in sessions:
    print(f"Session: {session['session_id']}")
    print(f"  Repository: {session['repository_path']}")
    print(f"  Created: {session['created_at']}")
    print(f"  Expires: {session['expires_at']}")
    print(f"  Active: {session['active']}")
```

## Contributing

### Adding New Features

1. Create feature branch
2. Implement feature with tests
3. Update documentation
4. Submit pull request

### Code Style

- Follow PEP 8
- Use type hints
- Add docstrings
- Write tests

### Pull Request Process

1. Ensure all tests pass
2. Update documentation
3. Add changelog entry
4. Request review

## References

- [Migration Guide](graphrag-migration-guide.md)
- [User Guide](user-guide-graphrag.md)
- [Architecture Documentation](graphrag-repository-isolation.md)

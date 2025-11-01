# Developer Guide: GraphRAG Repository Isolation

## System Architecture

### Core Components

1. **GraphRAGMCPIntegration**: Session management and context handling
2. **GraphRAGIndexManager**: Repository-specific indexing logic
3. **CodeAnalysisTool**: Enhanced with repository context support
4. **BackwardCompatibilityLayer**: API compatibility maintenance

### Key Design Decisions

#### Collection Naming Strategy
```python
def _get_collection_name(repo_path: Path) -> str:
    """Generate deterministic collection name from repository path."""
    repo_hash = hashlib.sha256(str(repo_path.resolve()).encode()).hexdigest()[:16]
    return f"repo_{repo_hash}"
```

**Rationale:**
- Deterministic naming prevents duplicate collections
- Hash-based names avoid filesystem path limitations
- Human-readable prefix for debugging

#### Session Management
```python
class GraphRAGMCPSession:
    """Represents isolated context for single repository."""

    def __init__(self, session_id: str, repo_path: str):
        self.session_id = session_id
        self.repo_path = Path(repo_path).resolve()
        self.collection_name = self._generate_collection_name()
        self.repo_label = self._generate_repo_label()
        self.created_at = datetime.now()
        self.last_accessed = datetime.now()
```
## Design Goals

**Design Goals:**
- Thread safety for concurrent sessions
- Automatic cleanup of expired sessions
- Memory efficient session tracking

## Integration Points

### With Auto-Coder Backend
The CodexMCPClient automatically handles session management:

```python
class CodexMCPClient:
    def analyze_function(self, function_name: str):
        # Automatic session creation if not exists
        if not hasattr(self, 'session_id'):
            self.session_id = self._create_session()

        # Use session context for all operations
        return self._mcp_call('find_symbol', {
            'fqname': function_name,
            'session_id': self.session_id
        })
```

### With GraphRAG Indexer
Repository isolation integrated into indexing pipeline:

```python
class GraphRAGIndexManager:
    def update_index(self, force: bool = False):
        # Use repository-specific collection
        collection_name = self._get_repository_collection_name()

        # Store with repository context
        self._store_embeddings_in_qdrant(graph_data, collection_name)
        self._store_graph_in_neo4j(graph_data, self.repo_label)
```

### With MCP Server
All MCP tools enhanced with optional session support:

```python
@mcp.tool()
def find_symbol(fqname: str, session_id: str = None) -> dict:
    # Backward compatibility: no session = global search
    if not session_id:
        return _legacy_find_symbol(fqname)

    # New isolation: use session context
    session = get_session(session_id)
    return find_symbol_in_repo(fqname, session.repo_label)
```

## Implementation Details

### Session Lifecycle

```python
class SessionManager:
    """Manages GraphRAG repository isolation sessions."""

    def __init__(self):
        self.sessions: Dict[str, GraphRAGMCPSession] = {}
        self._lock = threading.RLock()

    def create_session(self, repo_path: str) -> str:
        """Create new isolated session for repository."""
        with self._lock:
            session_id = self._generate_session_id()
            session = GraphRAGMCPSession(session_id, repo_path)
            self.sessions[session_id] = session
            return session_id

    def get_session(self, session_id: str) -> Optional[GraphRAGMCPSession]:
        """Get session by ID."""
        with self._lock:
            session = self.sessions.get(session_id)
            if session:
                session.last_accessed = datetime.now()
            return session

    def cleanup_expired_sessions(self, max_age_hours: int = 24):
        """Remove expired sessions."""
        with self._lock:
            now = datetime.now()
            expired = [
                sid for sid, session in self.sessions.items()
                if (now - session.last_accessed).total_seconds() > max_age_hours * 3600
            ]
            for sid in expired:
                del self.sessions[sid]
```

### Qdrant Integration

```python
class QdrantRepositoryManager:
    """Manages Qdrant collections for repository isolation."""

    def __init__(self, client: QdrantClient):
        self.client = client

    def _get_collection_name(self, repo_path: str) -> str:
        """Generate repository-specific collection name."""
        repo_hash = hashlib.sha256(repo_path.encode()).hexdigest()[:16]
        return f"repo_{repo_hash}"

    def create_collection(self, repo_path: str):
        """Create isolated collection for repository."""
        collection_name = self._get_collection_name(repo_path)

        # Check if collection exists
        if self.client.collection_exists(collection_name):
            return collection_name

        # Create collection with appropriate configuration
        self.client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(
                size=1536,  # OpenAI embedding size
                distance=Distance.COSINE
            )
        )
        return collection_name

    def search(self, query_vector: List[float], collection_name: str, limit: int = 10):
        """Search within repository-specific collection."""
        return self.client.search(
            collection_name=collection_name,
            query_vector=query_vector,
            limit=limit
        )
```

### Neo4j Integration

```python
class Neo4jRepositoryManager:
    """Manages Neo4j repository isolation."""

    def __init__(self, driver: neo4j.Driver):
        self.driver = driver

    def _get_repo_label(self, repo_path: str) -> str:
        """Generate repository-specific label."""
        repo_hash = hashlib.sha256(repo_path.encode()).hexdigest().upper()
        return f"Repo_{repo_hash}"

    def store_graph_data(self, graph_data: dict, repo_path: str):
        """Store graph data with repository label."""
        repo_label = self._get_repo_label(repo_path)

        with self.driver.session() as session:
            # Create nodes with repository label
            for node in graph_data['nodes']:
                session.run(
                    f"""
                    MERGE (n:{repo_label}:CodeNode {{
                        id: $id,
                        name: $name,
                        type: $type,
                        file: $file,
                        line: $line
                    }})
                    """,
                    id=node['id'],
                    name=node['name'],
                    type=node['type'],
                    file=node.get('file'),
                    line=node.get('line')
                )

            # Create relationships
            for edge in graph_data['edges']:
                session.run(
                    f"""
                    MATCH (src:{repo_label} {{id: $src_id}})
                    MATCH (dst:{repo_label} {{id: $dst_id}})
                    MERGE (src)-[r:CALLS {{
                        type: $rel_type
                    }}]->(dst)
                    """,
                    src_id=edge['source'],
                    dst_id=edge['target'],
                    rel_type=edge.get('type', 'calls')
                )

    def query_graph(self, repo_path: str, query: str):
        """Query graph within repository context."""
        repo_label = self._get_repo_label(repo_path)

        with self.driver.session() as session:
            result = session.run(
                f"""
                MATCH (n:{repo_label}:CodeNode)
                WHERE n.name CONTAINS $query
                RETURN n
                LIMIT 20
                """,
                query=query
            )
            return [record['n'] for record in result]
```

## Testing Strategy

### Unit Tests

```python
def test_session_creation():
    """Test session creation and management."""
    manager = SessionManager()

    # Create session
    session_id = manager.create_session("/path/to/repo")
    assert session_id is not None
    assert session_id in manager.sessions

    # Get session
    session = manager.get_session(session_id)
    assert session is not None
    assert session.repo_path == "/path/to/repo"

    # Cleanup
    manager.cleanup_expired_sessions()
    assert session_id not in manager.sessions

def test_collection_naming():
    """Test deterministic collection naming."""
    repo_path = "/path/to/repo"
    name1 = QdrantRepositoryManager._get_collection_name(repo_path)
    name2 = QdrantRepositoryManager._get_collection_name(repo_path)
    assert name1 == name2  # Deterministic

    # Different repo, different name
    repo_path2 = "/different/repo"
    name3 = QdrantRepositoryManager._get_collection_name(repo_path2)
    assert name1 != name3

def test_repo_label_generation():
    """Test repository label generation."""
    repo_path = "/path/to/repo"
    label1 = Neo4jRepositoryManager._get_repo_label(repo_path)
    label2 = Neo4jRepositoryManager._get_repo_label(repo_path)
    assert label1 == label2  # Deterministic
    assert label1.startswith("Repo_")  # Proper format
```

### Integration Tests

```python
def test_session_isolation():
    """Test that sessions are properly isolated."""
    integration = GraphRAGMCPIntegration()

    # Create two sessions
    session1 = integration.create_session("/repo/a")
    session2 = integration.create_session("/repo/b")

    # Verify isolation
    assert session1 != session2
    assert session1 not in integration.get_session(session2).repo_path
    assert session2 not in integration.get_session(session1).repo_path

def test_search_isolation():
    """Test that searches return repository-specific results."""
    integration = GraphRAGMCPIntegration()

    # Index two repositories with different content
    repo_a_session = integration.create_session("/repo/a")
    repo_b_session = integration.create_session("/repo/b")

    # Add data to repository A
    integration.add_symbol("function_a", session_id=repo_a_session)

    # Add data to repository B
    integration.add_symbol("function_b", session_id=repo_b_session)

    # Search in repository A should not find function_b
    results_a = integration.search("function", session_id=repo_a_session)
    assert "function_b" not in [r.name for r in results_a]

    # Search in repository B should not find function_a
    results_b = integration.search("function", session_id=repo_b_session)
    assert "function_a" not in [r.name for r in results_b]

def test_backward_compatibility():
    """Test backward compatibility without session_id."""
    # Legacy code without session should still work
    result = mcp_tool.find_symbol("function_name")
    assert result is not None

    # But should show deprecation warning
    assert "deprecation" in caplog.text.lower()
```

## Performance Considerations

### Memory Management

```python
class GraphRAGMCPSession:
    """Optimized session with memory management."""

    def __init__(self, session_id: str, repo_path: str):
        self.session_id = session_id
        self.repo_path = repo_path
        self.collection_name = self._generate_collection_name()
        self.repo_label = self._generate_repo_label()
        self.created_at = datetime.now()
        self.last_accessed = datetime.now()

        # LRU cache for frequently accessed symbols
        self._symbol_cache = collections.LRUCache(maxsize=1000)

        # Use weak references to prevent memory leaks
        self._context_refs: List[weakref.WeakSet] = []
```

### Query Optimization

```python
class OptimizedQueryEngine:
    """Optimized query engine with caching and batching."""

    def __init__(self):
        self._query_cache = TTLCache(maxsize=1000, ttl=300)  # 5 min TTL
        self._pending_queries = defaultdict(list)
        self._batch_timer = None

    def search(self, query: str, session_id: str, **kwargs) -> SearchResults:
        # Check cache first
        cache_key = self._make_cache_key(query, session_id, **kwargs)
        if cache_key in self._query_cache:
            return self._query_cache[cache_key]

        # Batch queries for efficiency
        self._batch_query(query, session_id, **kwargs)

        # Return cached or future result
        return self._get_cached_or_pending_result(cache_key)

    def _batch_query(self, query: str, session_id: str, **kwargs):
        """Batch multiple queries for efficient execution."""
        # Group similar queries
        self._pending_queries[session_id].append({
            'query': query,
            'kwargs': kwargs,
            'timestamp': time.time()
        })

        # Execute batch after short delay
        if not self._batch_timer:
            self._batch_timer = threading.Timer(0.1, self._execute_batched_queries)
            self._batch_timer.start()
```

### Concurrency Handling

```python
from concurrent.futures import ThreadPoolExecutor
import asyncio

class ConcurrentSessionManager:
    """Thread-safe session manager with concurrent support."""

    def __init__(self, max_workers: int = 10):
        self.sessions: Dict[str, GraphRAGMCPSession] = {}
        self._lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=max_workers)

    async def process_repository(self, repo_path: str, operations: List[Operation]):
        """Process repository operations concurrently."""
        session_id = self.create_session(repo_path)

        # Execute operations concurrently
        tasks = [
            self._execute_operation(op, session_id)
            for op in operations
        ]

        results = await asyncio.gather(*tasks)

        # Cleanup session
        self.cleanup_session(session_id)

        return results
```

## Error Handling and Resilience

### Graceful Degradation

```python
class ResilientMCPClient:
    """MCP client with graceful degradation."""

    def __init__(self):
        self.retry_count = 3
        self.retry_delay = 1.0

    def find_symbol(self, fqname: str, session_id: str = None) -> Optional[dict]:
        """Find symbol with fallback to legacy mode."""
        for attempt in range(self.retry_count):
            try:
                return self._mcp_call('find_symbol', {
                    'fqname': fqname,
                    'session_id': session_id
                })
            except SessionNotFoundError:
                # Fallback to legacy mode
                logger.warning(f"Session {session_id} not found, falling back to legacy mode")
                return self._legacy_find_symbol(fqname)
            except Exception as e:
                if attempt == self.retry_count - 1:
                    raise
                time.sleep(self.retry_delay * (2 ** attempt))
```

### Health Checks

```python
class SessionHealthMonitor:
    """Monitor session health and performance."""

    def __init__(self, manager: SessionManager):
        self.manager = manager
        self.metrics = defaultdict(list)

    def check_session_health(self, session_id: str) -> HealthStatus:
        """Check health of specific session."""
        session = self.manager.get_session(session_id)
        if not session:
            return HealthStatus.NOT_FOUND

        # Check session age
        age = (datetime.now() - session.created_at).total_seconds()
        if age > SESSION_MAX_AGE:
            return HealthStatus.EXPIRED

        # Check activity
        inactive_time = (datetime.now() - session.last_accessed).total_seconds()
        if inactive_time > SESSION_INACTIVITY_TIMEOUT:
            return HealthStatus.INACTIVE

        return HealthStatus.HEALTHY

    def monitor_all_sessions(self):
        """Monitor all active sessions."""
        unhealthy_sessions = []

        for session_id in list(self.manager.sessions.keys()):
            health = self.check_session_health(session_id)
            if health != HealthStatus.HEALTHY:
                unhealthy_sessions.append((session_id, health))

        # Cleanup unhealthy sessions
        for session_id, health in unhealthy_sessions:
            logger.warning(f"Cleaning up {health.value} session {session_id}")
            self.manager.cleanup_session(session_id)
```

## Deployment Considerations

### Configuration

```yaml
# config/graphrag-isolation.yaml
repository_isolation:
  enabled: true
  session_timeout: 3600
  max_concurrent_sessions: 100
  collection_prefix: "repo_"
  cache_size: 1000

performance:
  max_workers: 10
  query_batch_size: 50
  query_batch_delay: 0.1
  cache_ttl: 300

health:
  session_max_age: 86400  # 24 hours
  session_inactivity_timeout: 3600  # 1 hour
  health_check_interval: 300  # 5 minutes

logging:
  level: INFO
  sessions: true
  queries: false
  performance: true
```

### Monitoring

```python
class SessionMetrics:
    """Collect and report session metrics."""

    def __init__(self):
        self.active_sessions = 0
        self.total_queries = 0
        self.query_times = []
        self.session_creations = 0
        self.session_expirations = 0

    def record_session_created(self):
        self.active_sessions += 1
        self.session_creations += 1

    def record_session_expired(self):
        self.active_sessions -= 1
        self.session_expirations += 1

    def record_query(self, duration: float):
        self.total_queries += 1
        self.query_times.append(duration)

    def get_stats(self) -> dict:
        return {
            'active_sessions': self.active_sessions,
            'total_queries': self.total_queries,
            'avg_query_time': sum(self.query_times) / len(self.query_times) if self.query_times else 0,
            'session_creations': self.session_creations,
            'session_expirations': self.session_expirations,
        }
```

## Best Practices

### 1. Session Management
- Always create sessions explicitly
- Clean up sessions when done
- Monitor session count and resource usage
- Use appropriate session timeouts

### 2. Error Handling
- Implement retry logic for transient failures
- Provide fallback to legacy mode
- Log errors with sufficient context
- Monitor error rates and types

### 3. Performance Optimization
- Use query caching for frequently accessed data
- Batch operations when possible
- Monitor query performance
- Optimize collection and label naming

### 4. Testing
- Unit test all session management functions
- Integration test isolation guarantees
- Performance test with realistic workloads
- Load test concurrent session handling

### 5. Documentation
- Document all API changes
- Provide migration examples
- Include troubleshooting guides
- Maintain change logs
>>>>>>> 7dc5c55e7030dbc8f587adc70f5dcfbe0a15b3ac

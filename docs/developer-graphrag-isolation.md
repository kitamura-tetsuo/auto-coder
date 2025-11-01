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

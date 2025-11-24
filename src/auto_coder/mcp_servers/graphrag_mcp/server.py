from typing import Optional

from graphrag_mcp.code_analysis_tool import CodeAnalysisTool
from mcp.server.fastmcp import Context, FastMCP

# Create an MCP server
mcp = FastMCP(
    "GraphRAG Code Analysis",
    dependencies=["neo4j", "qdrant-client", "sentence-transformers"],
)

# Initialize the code analysis tool
code_tool = CodeAnalysisTool()


def _get_session_context(session_id: str) -> Optional[dict]:
    """Get session context by session_id.

    Args:
        session_id: Session identifier

    Returns:
        Session context dictionary or None if not found
    """
    # In a full implementation, this would retrieve from a session store
    return {"session_id": session_id}


def _get_repo_label_for_session(session_id: str) -> Optional[str]:
    """Get repository label for a session.

    Args:
        session_id: Session identifier

    Returns:
        Repository label string or None if session not found
    """
    import hashlib

    repo_hash = hashlib.sha256(session_id.encode()).hexdigest()[:8].upper()
    return f"Repo_{repo_hash}"


@mcp.tool()
def get_api_version() -> dict:
    """Get API version and session information.

    Returns:
        Dictionary with version and feature information
    """
    return {
        "api_version": "2.0.0",
        "session_support": True,
        "repository_isolation": True,
        "features": {
            "session_management": "2.0+",
            "repository_isolation": "2.0+",
        },
    }


@mcp.tool()
def find_symbol(fqname: str, session_id: str) -> dict:
    """Find symbol with repository isolation.

    This tool searches the TypeScript/JavaScript code graph for a specific symbol
    (function, method, class, interface, or type) by its fully qualified name.

    Args:
        fqname: Fully qualified name (e.g., 'src/utils.ts::calculateHash' or 'src/models/User.ts::User::getName')
        session_id: Session ID for repository isolation

    Returns:
        Symbol details including:
        - id: Unique identifier
        - kind: Symbol type (Function, Method, Class, Interface, Type)
        - fqname: Fully qualified name
        - signature: Function/method signature
        - short_summary: Brief description
        - complexity: Cyclomatic complexity
        - tokens_est: Estimated token count
        - file: Source file path
        - start_line, end_line: Source location
        - tags: Associated tags

    Example:
        find_symbol("src/utils/hash.ts::calculateHash", session_id="session_123")
    """
    session = _get_session_context(session_id)
    if not session:
        return {"error": "Invalid session_id"}

    repo_label = _get_repo_label_for_session(session_id)
    return code_tool.find_symbol(fqname, repo_label=repo_label)


@mcp.tool()
def get_call_graph(symbol_id: str, session_id: str, direction: str = "both", depth: int = 1) -> dict:
    """Get call graph with repository isolation.

    This tool analyzes the call relationships in the code graph, showing which
    functions/methods call the target symbol (callers) and which functions/methods
    the target symbol calls (callees).

    Args:
        symbol_id: Symbol ID (obtained from find_symbol)
        session_id: Session ID for repository isolation
        direction: 'callers' (who calls this), 'callees' (what this calls), or 'both' (default: 'both')
        depth: Traversal depth 1-3 (default: 1). Higher depth shows indirect relationships.

    Returns:
        Call graph with:
        - nodes: List of related symbols with their details
        - edges: List of call relationships with call counts

    Example:
        get_call_graph("symbol_123", "session_123", direction="callers", depth=2)
        get_call_graph("symbol_123", "session_123")
    """
    session = _get_session_context(session_id)
    if not session:
        return {"error": "Invalid session_id"}

    repo_label = _get_repo_label_for_session(session_id)
    return code_tool.get_call_graph(symbol_id, direction, depth, repo_label=repo_label)


@mcp.tool()
def get_dependencies(file_path: str, session_id: str) -> dict:
    """Get file dependencies with repository isolation.

    This tool analyzes the import graph to show which files a given file imports
    and which files import the given file.

    Args:
        file_path: File path (e.g., 'src/utils.ts')
        session_id: Session ID for repository isolation

    Returns:
        Dependency information:
        - imports: List of files this file imports (with import counts)
        - imported_by: List of files that import this file (with import counts)

    Example:
        get_dependencies("src/utils/hash.ts", session_id="session_123")
    """
    session = _get_session_context(session_id)
    if not session:
        return {"error": "Invalid session_id"}

    repo_label = _get_repo_label_for_session(session_id)
    return code_tool.get_dependencies(file_path, repo_label=repo_label)


@mcp.tool()
def impact_analysis(symbol_ids: list, session_id: str, max_depth: int = 2) -> dict:
    """Analyze impact with repository isolation.

    This tool performs comprehensive impact analysis by traversing the code graph
    to find all symbols and files that would be affected by changes to the specified
    symbols. It considers:
    - Direct and indirect callers
    - Files that import the symbols' files
    - Symbols that extend or implement the changed symbols

    Args:
        symbol_ids: List of symbol IDs to analyze (obtained from find_symbol)
        session_id: Session ID for repository isolation
        max_depth: Maximum traversal depth 1-3 (default: 2)

    Returns:
        Impact analysis:
        - affected_symbols: List of symbols that would be affected
        - affected_files: List of files that would be affected
        - impact_summary: Summary statistics (total counts, breakdown by kind)

    Example:
        impact_analysis(["symbol_123", "symbol_456"], "session_123", max_depth=2)
        impact_analysis(["symbol_123"], "session_123")
    """
    session = _get_session_context(session_id)
    if not session:
        return {"error": "Invalid session_id"}

    repo_label = _get_repo_label_for_session(session_id)
    return code_tool.impact_analysis(symbol_ids, max_depth, repo_label=repo_label)


@mcp.tool()
def semantic_code_search(query: str, session_id: str, limit: int = 10, kind_filter: Optional[list] = None) -> dict:
    """Semantic search with repository isolation.

    This tool uses vector embeddings to find code symbols that are semantically
    similar to your natural language query. Useful for finding relevant code
    when you don't know the exact symbol names.

    Args:
        query: Natural language description of what you're looking for
               (e.g., "functions that calculate hash values" or "user authentication methods")
        session_id: Session ID for repository isolation
        limit: Maximum number of results to return (default: 10)
        kind_filter: Optional list of symbol kinds to filter results
                    (e.g., ['Function', 'Class', 'Method'])

    Returns:
        Semantically similar symbols:
        - symbols: List of matching symbols with similarity scores

    Example:
        semantic_code_search("hash calculation functions", "session_123", limit=5, kind_filter=["Function"])
        semantic_code_search("authentication", "session_123")
    """
    session = _get_session_context(session_id)
    if not session:
        return {"error": "Invalid session_id"}

    repo_label = _get_repo_label_for_session(session_id)
    return code_tool.semantic_code_search_in_collection(query, repo_label, limit)


@mcp.resource("https://graphrag.db/schema/neo4j")
def get_graph_schema() -> str:
    """
    Get the Neo4j graph schema for TypeScript/JavaScript code structure.

    This resource provides detailed information about the code graph schema,
    including node types, relationship types, and property definitions.

    Node Labels:
    - File: Source file (TypeScript/JavaScript)
    - Function: Top-level function declaration
    - Method: Class method
    - Class: Class declaration
    - Interface: Interface declaration
    - Type: Type alias declaration

    Relationship Types:
    - CONTAINS: File contains symbols (File -> Symbol)
    - CALLS: Function/method calls another (Symbol -> Symbol)
    - EXTENDS: Class extends another class (Class -> Class)
    - IMPLEMENTS: Class implements interface (Class -> Interface)
    - IMPORTS: File imports from another file (File -> File)

    Node Properties:
    - id: Unique identifier (string)
    - kind: Node type (File/Function/Method/Class/Interface/Type)
    - fqname: Fully qualified name (string)
    - sig: Signature for functions/methods (string)
    - short: Short summary/description (string)
    - complexity: Cyclomatic complexity (integer)
    - tokens_est: Estimated token count (integer)
    - file: Source file path (string)
    - start_line: Starting line number (integer)
    - end_line: Ending line number (integer)
    - tags: Associated tags (list of strings)

    Edge Properties:
    - count: Number of times relationship occurs (integer)
    - locations: List of source locations where relationship occurs
    """
    try:
        schema = []
        with code_tool.neo4j_driver.session() as session:
            # Get node labels
            result = session.run(
                """
            CALL db.labels() YIELD label
            RETURN collect(label) as labels
            """
            )
            labels = result.single()["labels"]
            schema.append("Node Labels: " + ", ".join(labels))

            # Get relationship types
            result = session.run(
                """
            CALL db.relationshipTypes() YIELD relationshipType
            RETURN collect(relationshipType) as types
            """
            )
            rel_types = result.single()["types"]
            schema.append("Relationship Types: " + ", ".join(rel_types))

            # Get property keys
            result = session.run(
                """
            CALL db.propertyKeys() YIELD propertyKey
            RETURN collect(propertyKey) as keys
            """
            )
            prop_keys = result.single()["keys"]
            schema.append("Property Keys: " + ", ".join(prop_keys))

            # Get node count by label
            schema.append("\nNode Counts:")
            for label in labels:
                count_query = f"MATCH (n:{label}) RETURN count(n) as count"
                count = session.run(count_query).single()["count"]
                schema.append(f"  {label}: {count}")

        return "\n".join(schema)
    except Exception as e:
        return f"Error retrieving graph schema: {str(e)}"


@mcp.resource("https://graphrag.db/collection/qdrant")
def get_vector_collection_info() -> str:
    """
    Get information about the Qdrant vector collection for code embeddings.

    This resource provides information about the vector database used for
    semantic code search. Code symbols are embedded using sentence-transformers
    and stored in Qdrant for similarity search.

    Collection: code_chunks (default)
    Embedding Model: all-MiniLM-L6-v2
    Vector Size: 384 dimensions
    Distance Function: Cosine similarity

    Each vector point represents a code symbol with metadata:
    - id: Symbol ID (matches Neo4j node ID)
    - kind: Symbol type (Function/Method/Class/etc)
    - fqname: Fully qualified name
    - short: Short summary
    - file: Source file path
    - start_line: Starting line number
    """
    try:
        info = []
        collection_info = code_tool.qdrant_client.get_collection(code_tool.qdrant_collection)

        # Try to extract vectors count based on client version
        vectors_count = 0
        if hasattr(collection_info, "vectors_count"):
            vectors_count = collection_info.vectors_count
        elif hasattr(collection_info, "points_count"):
            vectors_count = collection_info.points_count

        info.append(f"Collection: {code_tool.qdrant_collection}")
        info.append(f"Vectors Count: {vectors_count}")
        info.append(f"Embedding Model: {code_tool.model_name}")

        # Add vector configuration
        try:
            if hasattr(collection_info, "config"):
                if hasattr(collection_info.config, "params"):
                    vector_size = getattr(collection_info.config.params, "vector_size", "unknown")
                    info.append(f"Vector Size: {vector_size}")

                    distance = getattr(collection_info.config.params, "distance", "unknown")
                    info.append(f"Distance Function: {distance}")
        except:
            info.append("Could not retrieve detailed vector configuration")

        return "\n".join(info)
    except Exception as e:
        return f"Error retrieving vector collection info: {str(e)}"


if __name__ == "__main__":
    mcp.run()

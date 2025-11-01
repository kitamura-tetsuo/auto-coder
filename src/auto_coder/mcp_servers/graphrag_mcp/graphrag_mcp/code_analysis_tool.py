import hashlib
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from neo4j import GraphDatabase
from qdrant_client import QdrantClient
from sentence_transformers import SentenceTransformer

from auto_coder.backward_compatibility_layer import (
    BackwardCompatibilityLayer,
    get_compatibility_layer,
)

# Configure logging to write to a file instead of stdout
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    filename="graphrag.log",
    filemode="a",
)
logger = logging.getLogger("graphrag")


class CodeAnalysisTool:
    """MCP Tool for querying TypeScript/JavaScript code structure using GraphRAG."""

    def __init__(
        self,
        repo_path: Optional[str] = None,
        compatibility_layer: Optional[BackwardCompatibilityLayer] = None,
    ):
        # Neo4j connection
        self.neo4j_uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        self.neo4j_user = os.getenv("NEO4J_USER", "neo4j")
        self.neo4j_password = os.getenv("NEO4J_PASSWORD", "password")
        self.neo4j_driver = None

        # Qdrant connection
        self.qdrant_host = os.getenv("QDRANT_HOST", "localhost")
        self.qdrant_port = int(os.getenv("QDRANT_PORT", "6333"))
        self.qdrant_collection = os.getenv("QDRANT_COLLECTION", "code_chunks")
        self.qdrant_client = None

        # Embedding model
        self.model_name = "all-MiniLM-L6-v2"
        self.model = None

        # Connection state
        self._connected = False

        # Repository path for label filtering
        self.repo_path = Path(repo_path) if repo_path else Path.cwd()
        self._repo_label = None
        self._repo_hash = None

        # Backward compatibility layer
        self.compat_layer = compatibility_layer or get_compatibility_layer()

        # Don't connect immediately - wait until first use
        # This allows the MCP server to start even if Neo4j/Qdrant are not running

    def _get_session_label(self, session_id: str) -> str:
        """Get session-specific label for Neo4j queries.

        Args:
            session_id: Session identifier

        Returns:
            Session label in format Session_{HASH}
        """
        return self.compat_layer.get_repo_label(session_id)

    def _get_repo_hash(self, session_id: Optional[str] = None) -> str:
        """Get repository hash for a session.

        Args:
            session_id: Optional session ID. If None, uses default from repo_path.

        Returns:
            8-character MD5 hash of session_id or repo_path
        """
        if session_id:
            # Use session_id directly
            return hashlib.md5(session_id.encode()).hexdigest()[:8]
        else:
            # Use repo_path for backward compatibility
            repo_path_str = str(self.repo_path.resolve())
            return hashlib.md5(repo_path_str.encode()).hexdigest()[:8]

    def _ensure_connected(self):
        """Ensure connections are established (lazy initialization)."""
        if self._connected:
            return

        self._connect()
        self._connected = True

    def _connect(self):
        """Establish connections to Neo4j and Qdrant."""
        # Connect to Neo4j
        try:
            self.neo4j_driver = GraphDatabase.driver(
                self.neo4j_uri, auth=(self.neo4j_user, self.neo4j_password)
            )
            # Test connection
            with self.neo4j_driver.session() as session:
                result = session.run("MATCH (f:File) RETURN count(f) AS count")
                record = result.single()
                logger.info(f"Connected to Neo4j with {record['count']} files")
        except Exception as e:
            logger.error(f"Neo4j connection error: {e}")
            raise RuntimeError(
                f"Failed to connect to Neo4j at {self.neo4j_uri}. Please ensure Neo4j is running. Error: {e}"
            )

        # Connect to Qdrant
        try:
            self.qdrant_client = QdrantClient(
                host=self.qdrant_host, port=self.qdrant_port
            )
            collection_info = self.qdrant_client.get_collection(self.qdrant_collection)

            # Check for vectors count based on client version
            vectors_count = 0
            if hasattr(collection_info, "vectors_count"):
                vectors_count = collection_info.vectors_count
            elif hasattr(collection_info, "points_count"):
                vectors_count = collection_info.points_count

            logger.info(
                f"Connected to Qdrant collection '{self.qdrant_collection}' with {vectors_count} vectors"
            )
        except Exception as e:
            logger.error(f"Qdrant connection error: {e}")
            raise RuntimeError(
                f"Failed to connect to Qdrant at {self.qdrant_host}:{self.qdrant_port}. Please ensure Qdrant is running. Error: {e}"
            )

        # Load the embedding model
        try:
            self.model = SentenceTransformer(self.model_name)
            logger.info(f"Loaded embedding model: {self.model_name}")
        except Exception as e:
            logger.error(f"Error loading embedding model: {e}")
            raise RuntimeError(
                f"Failed to load embedding model '{self.model_name}'. Error: {e}"
            )

    def find_symbol(
        self, fqname: str, session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Find a code symbol by fully qualified name.

        Args:
            fqname: Fully qualified name (e.g., 'src/utils.ts::calculateHash')
            session_id: Optional session identifier for repository isolation.
                       If not provided, will auto-generate from repo_path.

        Returns:
            Symbol details including id, kind, signature, complexity, location
        """
        result = {
            "fqname": fqname,
            "symbol": None,
            "error": None,
            "session_id": session_id,
        }

        try:
            self._ensure_connected()
        except Exception as e:
            result["error"] = str(e)
            return result

        if not self.neo4j_driver:
            result["error"] = "Neo4j connection not available"
            return result

        # Extract and validate session_id with backward compatibility
        session_id, is_legacy = self.compat_layer.extract_session_id(
            session_id=session_id, repo_path=str(self.repo_path)
        )
        result["session_id"] = session_id

        # Emit deprecation warning if using legacy mode
        if is_legacy and self.compat_layer.should_emit_deprecation_warning(
            "find_symbol_no_session_id"
        ):
            import warnings

            warnings.warn(
                "Calling find_symbol without session_id is deprecated. "
                "Please provide session_id for better repository isolation. "
                f"Auto-generated session_id: {session_id}",
                DeprecationWarning,
                stacklevel=2,
            )
            self.compat_layer.mark_warning_shown("find_symbol_no_session_id")

        session_label = self._get_session_label(session_id)
        repo_hash = self._get_repo_hash(session_id)
        repo_path_str = str(self.repo_path.resolve())

        try:
            with self.neo4j_driver.session() as session:
                # Query with session label filtering (for labeled nodes)
                # Also query unlabeled nodes for backward compatibility
                cypher_query = f"""
                MATCH (s)
                WHERE (
                    (s:{session_label}:CodeNode AND s.repo_hash = $repo_hash)
                    OR
                    (s:CodeNode AND NOT s:{session_label} AND s.repo_path = $repo_path)
                )
                AND s.fqname = $fqname
                AND s.kind IN ['Function', 'Method', 'Class', 'Interface', 'Type']
                RETURN s.id as id, s.kind as kind, s.fqname as fqname,
                       s.sig as signature, s.short as short_summary,
                       s.complexity as complexity, s.tokens_est as tokens_est,
                       s.file as file, s.start_line as start_line, s.end_line as end_line,
                       s.tags as tags
                """

                query_result = session.run(
                    cypher_query,
                    fqname=fqname,
                    repo_hash=repo_hash,
                    repo_path=repo_path_str,
                )
                record = query_result.single()

                if record:
                    result["symbol"] = {
                        "id": record["id"],
                        "kind": record["kind"],
                        "fqname": record["fqname"],
                        "signature": record["signature"],
                        "short_summary": record["short_summary"],
                        "complexity": record["complexity"],
                        "tokens_est": record["tokens_est"],
                        "file": record["file"],
                        "start_line": record["start_line"],
                        "end_line": record["end_line"],
                        "tags": record["tags"] or [],
                    }
                else:
                    result["error"] = f"Symbol '{fqname}' not found"

        except Exception as e:
            result["error"] = f"Neo4j query error: {e}"

        return result

    def get_call_graph(
        self,
        symbol_id: str,
        direction: str = "both",
        depth: int = 1,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get call graph for a symbol.

        Args:
            symbol_id: Symbol ID
            direction: 'callers' (who calls this), 'callees' (what this calls), or 'both'
            depth: Traversal depth (1-3)
            session_id: Optional session identifier for repository isolation.
                       If not provided, will auto-generate from repo_path.

        Returns:
            Call graph with nodes and edges
        """
        result = {
            "symbol_id": symbol_id,
            "direction": direction,
            "depth": depth,
            "nodes": [],
            "edges": [],
            "error": None,
            "session_id": session_id,
        }

        try:
            self._ensure_connected()
        except Exception as e:
            result["error"] = str(e)
            return result

        if not self.neo4j_driver:
            result["error"] = "Neo4j connection not available"
            return result

        if depth < 1 or depth > 3:
            result["error"] = "Depth must be between 1 and 3"
            return result

        # Extract and validate session_id with backward compatibility
        session_id, is_legacy = self.compat_layer.extract_session_id(
            session_id=session_id, repo_path=str(self.repo_path)
        )
        result["session_id"] = session_id

        # Emit deprecation warning if using legacy mode
        if is_legacy and self.compat_layer.should_emit_deprecation_warning(
            "get_call_graph_no_session_id"
        ):
            import warnings

            warnings.warn(
                "Calling get_call_graph without session_id is deprecated. "
                "Please provide session_id for better repository isolation. "
                f"Auto-generated session_id: {session_id}",
                DeprecationWarning,
                stacklevel=2,
            )
            self.compat_layer.mark_warning_shown("get_call_graph_no_session_id")

        session_label = self._get_session_label(session_id)
        repo_hash = self._get_repo_hash(session_id)
        repo_path_str = str(self.repo_path.resolve())

        try:
            with self.neo4j_driver.session() as session:
                # Build Cypher query based on direction with session label filtering
                if direction == "callers":
                    cypher_query = f"""
                    MATCH (s)
                    WHERE (
                        (s:{session_label}:CodeNode AND s.id = $symbol_id AND s.repo_hash = $repo_hash)
                        OR
                        (s:CodeNode AND NOT s:{session_label} AND s.id = $symbol_id AND s.repo_path = $repo_path)
                    )
                    MATCH path = (caller)-[:CALLS*1..{depth}]->(s)
                    WHERE (
                        (caller:{session_label}:CodeNode AND caller.repo_hash = $repo_hash)
                        OR
                        (caller:CodeNode AND NOT caller:{session_label} AND caller.repo_path = $repo_path)
                    )
                    WITH caller, s, relationships(path) as rels
                    RETURN DISTINCT caller.id as id, caller.kind as kind, caller.fqname as fqname,
                           caller.file as file, caller.start_line as start_line,
                           [r IN rels | {{from: startNode(r).id, to: endNode(r).id, count: r.count}}] as edges
                    """
                elif direction == "callees":
                    cypher_query = f"""
                    MATCH (s)
                    WHERE (
                        (s:{session_label}:CodeNode AND s.id = $symbol_id AND s.repo_hash = $repo_hash)
                        OR
                        (s:CodeNode AND NOT s:{session_label} AND s.id = $symbol_id AND s.repo_path = $repo_path)
                    )
                    MATCH path = (s)-[:CALLS*1..{depth}]->(callee)
                    WHERE (
                        (callee:{session_label}:CodeNode AND callee.repo_hash = $repo_hash)
                        OR
                        (callee:CodeNode AND NOT callee:{session_label} AND callee.repo_path = $repo_path)
                    )
                    WITH callee, s, relationships(path) as rels
                    RETURN DISTINCT callee.id as id, callee.kind as kind, callee.fqname as fqname,
                           callee.file as file, callee.start_line as start_line,
                           [r IN rels | {{from: startNode(r).id, to: endNode(r).id, count: r.count}}] as edges
                    """
                else:  # both
                    cypher_query = f"""
                    MATCH (s)
                    WHERE (
                        (s:{session_label}:CodeNode AND s.id = $symbol_id AND s.repo_hash = $repo_hash)
                        OR
                        (s:CodeNode AND NOT s:{session_label} AND s.id = $symbol_id AND s.repo_path = $repo_path)
                    )
                    OPTIONAL MATCH path1 = (caller)-[:CALLS*1..{depth}]->(s)
                    WHERE (
                        (caller:{session_label}:CodeNode AND caller.repo_hash = $repo_hash)
                        OR
                        (caller:CodeNode AND NOT caller:{session_label} AND caller.repo_path = $repo_path)
                    )
                    OPTIONAL MATCH path2 = (s)-[:CALLS*1..{depth}]->(callee)
                    WHERE (
                        (callee:{session_label}:CodeNode AND callee.repo_hash = $repo_hash)
                        OR
                        (callee:CodeNode AND NOT callee:{session_label} AND callee.repo_path = $repo_path)
                    )
                    WITH s, caller, callee, relationships(path1) + relationships(path2) as rels
                    WHERE caller IS NOT NULL OR callee IS NOT NULL
                    WITH COALESCE(caller, callee) as related, rels
                    RETURN DISTINCT related.id as id, related.kind as kind, related.fqname as fqname,
                           related.file as file, related.start_line as start_line,
                           [r IN rels WHERE r IS NOT NULL | {{from: startNode(r).id, to: endNode(r).id, count: r.count}}] as edges
                    """

                query_result = session.run(
                    cypher_query,
                    symbol_id=symbol_id,
                    repo_hash=repo_hash,
                    repo_path=repo_path_str,
                )

                for record in query_result:
                    result["nodes"].append(
                        {
                            "id": record["id"],
                            "kind": record["kind"],
                            "fqname": record["fqname"],
                            "file": record["file"],
                            "start_line": record["start_line"],
                        }
                    )

                    # Add edges
                    for edge in record["edges"]:
                        if edge and edge not in result["edges"]:
                            result["edges"].append(edge)

                if not result["nodes"]:
                    result["error"] = f"No {direction} found for symbol '{symbol_id}'"

        except Exception as e:
            result["error"] = f"Neo4j query error: {e}"

        return result

    def get_dependencies(
        self, file_path: str, session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get file dependencies (imports).

        Args:
            file_path: File path (e.g., 'src/utils.ts')
            session_id: Optional session identifier for repository isolation.
                       If not provided, will auto-generate from repo_path.

        Returns:
            List of imported files and symbols
        """
        result = {"file": file_path, "imports": [], "imported_by": [], "error": None}

        try:
            self._ensure_connected()
        except Exception as e:
            result["error"] = str(e)
            return result

        if not self.neo4j_driver:
            result["error"] = "Neo4j connection not available"
            return result

        repo_label = self._get_repo_label()
        repo_hash = self._get_repo_hash()

        try:
            with self.neo4j_driver.session() as session:
                # Get imports (what this file imports)
                cypher_query = f"""
                MATCH (f)
                WHERE (
                    (f:{repo_label}:File AND f.file = $file_path AND f.repo_hash = $repo_hash)
                    OR
                    (f:File AND NOT f:{repo_label} AND f.file = $file_path AND f.repo_path = $repo_path)
                )
                MATCH (f)-[r:IMPORTS]->(imported:File)
                WHERE (
                    (imported:{repo_label}:File AND imported.repo_hash = $repo_hash)
                    OR
                    (imported:File AND NOT imported:{repo_label} AND imported.repo_path = $repo_path)
                )
                RETURN imported.file as file, r.count as count
                ORDER BY count DESC
                """

                query_result = session.run(
                    cypher_query,
                    file_path=file_path,
                    repo_hash=repo_hash,
                    repo_path=str(self.repo_path.resolve()),
                )
                for record in query_result:
                    result["imports"].append(
                        {"file": record["file"], "count": record["count"]}
                    )

                # Get imported_by (what files import this)
                cypher_query = f"""
                MATCH (importer:File)
                WHERE (
                    (importer:{repo_label}:File AND importer.repo_hash = $repo_hash)
                    OR
                    (importer:File AND NOT importer:{repo_label} AND importer.repo_path = $repo_path)
                )
                MATCH (importer)-[r:IMPORTS]->(f)
                WHERE (
                    (f:{repo_label}:File AND f.file = $file_path AND f.repo_hash = $repo_hash)
                    OR
                    (f:File AND NOT f:{repo_label} AND f.file = $file_path AND f.repo_path = $repo_path)
                )
                RETURN importer.file as file, r.count as count
                ORDER BY count DESC
                """

                query_result = session.run(
                    cypher_query,
                    file_path=file_path,
                    repo_hash=repo_hash,
                    repo_path=str(self.repo_path.resolve()),
                )
                for record in query_result:
                    result["imported_by"].append(
                        {"file": record["file"], "count": record["count"]}
                    )

        except Exception as e:
            result["error"] = f"Neo4j query error: {e}"

        return result

    def impact_analysis(
        self,
        symbol_ids: List[str],
        max_depth: int = 2,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Analyze the impact of changing given symbols.

        Args:
            symbol_ids: List of symbol IDs to analyze
            max_depth: Maximum traversal depth for impact analysis (1-3)
            session_id: Optional session identifier for repository isolation.
                       If not provided, will auto-generate from repo_path.

        Returns:
            Impact analysis including affected symbols, files, and relationships
        """
        result = {
            "analyzed_symbols": symbol_ids,
            "max_depth": max_depth,
            "affected_symbols": [],
            "affected_files": set(),
            "impact_summary": {},
            "error": None,
        }

        try:
            self._ensure_connected()
        except Exception as e:
            result["error"] = str(e)
            return result

        if not self.neo4j_driver:
            result["error"] = "Neo4j connection not available"
            return result

        if max_depth < 1 or max_depth > 3:
            result["error"] = "max_depth must be between 1 and 3"
            return result

        repo_label = self._get_repo_label()
        repo_hash = self._get_repo_hash()

        try:
            with self.neo4j_driver.session() as session:
                # Find all symbols that depend on the changed symbols with repo label filtering
                cypher_query = f"""
                MATCH (changed)
                WHERE (
                    (changed:{repo_label}:CodeNode AND changed.id IN $symbol_ids AND changed.repo_hash = $repo_hash)
                    OR
                    (changed:CodeNode AND NOT changed:{repo_label} AND changed.id IN $symbol_ids AND changed.repo_path = $repo_path)
                )

                // Find callers (symbols that call the changed symbols)
                OPTIONAL MATCH (caller)-[:CALLS*1..{max_depth}]->(changed)
                WHERE (
                    (caller:{repo_label}:CodeNode AND caller.repo_hash = $repo_hash)
                    OR
                    (caller:CodeNode AND NOT caller:{repo_label} AND caller.repo_path = $repo_path)
                )
                AND caller.id NOT IN $symbol_ids

                // Find symbols in files that import changed symbols' files
                OPTIONAL MATCH (changed_file:File)
                WHERE (
                    (changed_file:{repo_label}:File AND changed_file.repo_hash = $repo_hash)
                    OR
                    (changed_file:File AND NOT changed_file:{repo_label} AND changed_file.repo_path = $repo_path)
                )
                OPTIONAL MATCH (changed_file)-[:CONTAINS]->(changed)
                OPTIONAL MATCH (importing_file:File)
                WHERE (
                    (importing_file:{repo_label}:File AND importing_file.repo_hash = $repo_hash)
                    OR
                    (importing_file:File AND NOT importing_file:{repo_label} AND importing_file.repo_path = $repo_path)
                )
                OPTIONAL MATCH (importing_file)-[:IMPORTS*1..{max_depth}]->(changed_file)
                OPTIONAL MATCH (importing_file)-[:CONTAINS]->(importing_symbol)
                WHERE (
                    (importing_symbol:{repo_label}:CodeNode AND importing_symbol.repo_hash = $repo_hash)
                    OR
                    (importing_symbol:CodeNode AND NOT importing_symbol:{repo_label} AND importing_symbol.repo_path = $repo_path)
                )
                AND importing_symbol.id NOT IN $symbol_ids

                // Find symbols that extend/implement changed symbols
                OPTIONAL MATCH (implementer)-[:EXTENDS|IMPLEMENTS*1..{max_depth}]->(changed)
                WHERE (
                    (implementer:{repo_label}:CodeNode AND implementer.repo_hash = $repo_hash)
                    OR
                    (implementer:CodeNode AND NOT implementer:{repo_label} AND implementer.repo_path = $repo_path)
                )
                AND implementer.id NOT IN $symbol_ids

                WITH changed, caller, importing_symbol, implementer
                WHERE caller IS NOT NULL OR importing_symbol IS NOT NULL OR implementer IS NOT NULL

                WITH COALESCE(caller, importing_symbol, implementer) as affected
                WHERE affected IS NOT NULL

                RETURN DISTINCT affected.id as id, affected.kind as kind,
                       affected.fqname as fqname, affected.file as file,
                       affected.start_line as start_line, affected.end_line as end_line
                """

                query_result = session.run(
                    cypher_query,
                    symbol_ids=symbol_ids,
                    repo_hash=repo_hash,
                    repo_path=str(self.repo_path.resolve()),
                )

                for record in query_result:
                    affected_symbol = {
                        "id": record["id"],
                        "kind": record["kind"],
                        "fqname": record["fqname"],
                        "file": record["file"],
                        "start_line": record["start_line"],
                        "end_line": record["end_line"],
                    }
                    result["affected_symbols"].append(affected_symbol)
                    result["affected_files"].add(record["file"])

                # Convert set to list for JSON serialization
                result["affected_files"] = sorted(list(result["affected_files"]))

                # Generate summary
                result["impact_summary"] = {
                    "total_affected_symbols": len(result["affected_symbols"]),
                    "total_affected_files": len(result["affected_files"]),
                    "by_kind": {},
                }

                # Count by kind
                for symbol in result["affected_symbols"]:
                    kind = symbol["kind"]
                    result["impact_summary"]["by_kind"][kind] = (
                        result["impact_summary"]["by_kind"].get(kind, 0) + 1
                    )

        except Exception as e:
            result["error"] = f"Neo4j query error: {e}"

        return result

    def semantic_code_search(
        self,
        query: str,
        limit: int = 10,
        kind_filter: Optional[List[str]] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Search for code using semantic similarity.

        Args:
            query: Natural language query describing what you're looking for
            limit: Maximum number of results to return
            kind_filter: Optional list of symbol kinds to filter (e.g., ['Function', 'Class'])
            session_id: Optional session identifier for repository isolation.
                       If not provided, will auto-generate from repo_path.

        Returns:
            Semantically similar code symbols with scores
        """
        result = {"query": query, "symbols": [], "error": None}

        try:
            self._ensure_connected()
        except Exception as e:
            result["error"] = str(e)
            return result

        if self.model is None:
            try:
                self.model = SentenceTransformer(self.model_name)
            except Exception as e:
                result["error"] = f"Failed to load embedding model: {e}"
                return result

        repo_hash = self._get_repo_hash()

        # Generate embedding for query
        query_embedding = self.model.encode(query)

        # Search Qdrant with repository filtering
        try:
            # Get more results for filtering and repository-specific filtering
            search_result = self.qdrant_client.search(
                collection_name=self.qdrant_collection,
                query_vector=query_embedding.tolist(),
                limit=limit * 3,  # Get more results for filtering
                query_filter={
                    "must": [{"key": "repo_hash", "match": {"value": repo_hash}}]
                },
            )

            # Process results
            for result_item in search_result:
                symbol_id = result_item.id
                score = result_item.score

                # Get symbol details from payload or Neo4j
                if hasattr(result_item, "payload"):
                    payload = result_item.payload

                    # Apply kind filter if specified
                    if kind_filter and payload.get("kind") not in kind_filter:
                        continue

                    result["symbols"].append(
                        {
                            "id": symbol_id,
                            "kind": payload.get("kind"),
                            "fqname": payload.get("fqname"),
                            "short_summary": payload.get("short"),
                            "file": payload.get("file"),
                            "start_line": payload.get("start_line"),
                            "score": score,
                        }
                    )

                    if len(result["symbols"]) >= limit:
                        break

        except Exception as e:
            result["error"] = f"Qdrant search error: {e}"

        return result

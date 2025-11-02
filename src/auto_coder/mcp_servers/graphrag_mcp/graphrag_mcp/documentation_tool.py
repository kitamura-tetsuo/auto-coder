import logging
import os
from typing import Any, Dict, List, Optional, Union

from neo4j import GraphDatabase
from qdrant_client import QdrantClient
from sentence_transformers import SentenceTransformer

# Configure logging to write to a file instead of stdout
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    filename="graphrag.log",  # Log to a file instead of stdout
    filemode="a",
)
logger = logging.getLogger("graphrag")


class DocumentationGPTTool:
    """MCP Tool for querying the GraphRAG documentation system."""

    def __init__(self):
        # Neo4j connection
        self.neo4j_uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        self.neo4j_user = os.getenv("NEO4J_USER", "neo4j")
        self.neo4j_password = os.getenv("NEO4J_PASSWORD", "password")
        self.neo4j_driver = None

        # Qdrant connection
        self.qdrant_host = os.getenv("QDRANT_HOST", "localhost")
        self.qdrant_port = int(os.getenv("QDRANT_PORT", "6333"))
        self.qdrant_collection = os.getenv("QDRANT_COLLECTION", "document_chunks")
        self.qdrant_client = None

        # Embedding model
        self.model_name = "all-MiniLM-L6-v2"
        self.model = None

        # Initialize connections
        self._connect()

    def _connect(self):
        """Establish connections to Neo4j and Qdrant."""
        # Connect to Neo4j
        try:
            self.neo4j_driver = GraphDatabase.driver(
                self.neo4j_uri, auth=(self.neo4j_user, self.neo4j_password)
            )
            # Test connection
            with self.neo4j_driver.session() as session:
                result = session.run("MATCH (d:Document) RETURN count(d) AS count")
                record = result.single()
                logger.info(f"Connected to Neo4j with {record['count']} documents")
        except Exception as e:
            logger.error(f"Neo4j connection error: {e}")

        # Connect to Qdrant
        try:
            # Handle potential version compatibility issues
            try:
                self.qdrant_client = QdrantClient(
                    host=self.qdrant_host, port=self.qdrant_port
                )
                collection_info = self.qdrant_client.get_collection(
                    self.qdrant_collection
                )

                # Check for vectors count based on client version
                vectors_count = 0
                if hasattr(collection_info, "vectors_count"):
                    vectors_count = collection_info.vectors_count
                elif hasattr(collection_info, "points_count"):
                    vectors_count = collection_info.points_count
                else:
                    # Try to navigate the config structure based on observed variations
                    try:
                        if hasattr(collection_info.config, "params"):
                            if hasattr(collection_info.config.params, "vectors"):
                                vectors_count = (
                                    collection_info.config.params.vectors.size
                                )
<<<<<<< HEAD
                    except Exception:
=======
                    except:
>>>>>>> origin/issue-27
                        pass

                logger.info(
                    f"Connected to Qdrant collection '{self.qdrant_collection}' with {vectors_count} vectors"
                )
            except Exception as e:
                logger.warning(f"Qdrant connection warning: {e}")
                # Fallback for older versions if needed
        except Exception as e:
            logger.error(f"Qdrant connection error: {e}")

        # Load the embedding model
        try:
            self.model = SentenceTransformer(self.model_name)
            logger.info(f"Loaded embedding model: {self.model_name}")
        except Exception as e:
            logger.error(f"Error loading embedding model: {e}")

    def search_documentation(
        self, query: str, limit: int = 5, category: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Search for documentation using semantic search and optionally expand with graph context.

        Args:
            query: The search query
            limit: Maximum number of results to return
            category: Optional category filter

        Returns:
            Dictionary with search results and related documents
        """
        results = {"query": query, "chunks": [], "related_documents": []}

        # Generate embedding for query
        if self.model is None:
            try:
                self.model = SentenceTransformer(self.model_name)
            except Exception as e:
                results["error"] = f"Failed to load embedding model: {e}"
                return results

        query_embedding = self.model.encode(query)

        # Search Qdrant
        try:
            # Handle version compatibility issues with the search API
            try:
                # Newer versions use query_vector
                search_result = self.qdrant_client.search(
                    collection_name=self.qdrant_collection,
                    query_vector=query_embedding.tolist(),
                    limit=limit,
                )
            except TypeError:
                # Fall back to older versions using vector parameter
                search_result = self.qdrant_client.search(
                    collection_name=self.qdrant_collection,
                    vector=query_embedding.tolist(),
                    limit=limit,
                )

            # Add search results to response
            for result in search_result:
                # Extract ID and score
                chunk_id = result.id
                score = result.score

                # Get text content from payload
                text = ""
                if hasattr(result, "payload") and "text" in result.payload:
                    text = result.payload["text"]

                results["chunks"].append(
                    {"chunk_id": chunk_id, "text": text, "score": score}
                )
        except Exception as e:
            results["error"] = f"Qdrant search error: {e}"

        # Expand with related documents from Neo4j
        if self.neo4j_driver and len(results["chunks"]) > 0:
            try:
                with self.neo4j_driver.session() as session:
                    # Build a query to find documents containing these chunks
                    # and their related documents
                    chunk_ids = [chunk["chunk_id"] for chunk in results["chunks"]]

                    cypher_query = """
                    MATCH (c:Chunk)
                    WHERE c.id IN $chunk_ids
                    MATCH (c)-[:PART_OF]->(d:Document)
                    OPTIONAL MATCH (d)-[:RELATED_TO]->(related:Document)
                    WITH DISTINCT d, related
                    RETURN d.id as doc_id, d.title as title,
                           collect(DISTINCT {doc_id: related.id, title: related.title}) as related_docs
                    """

                    if category:
                        cypher_query = """
                        MATCH (c:Chunk)
                        WHERE c.id IN $chunk_ids
                        MATCH (c)-[:PART_OF]->(d:Document)-[:HAS_CATEGORY]->(cat:Category {name: $category})
                        OPTIONAL MATCH (d)-[:RELATED_TO]->(related:Document)
                        WITH DISTINCT d, related
                        RETURN d.id as doc_id, d.title as title,
                               collect(DISTINCT {doc_id: related.id, title: related.title}) as related_docs
                        """

                    result = session.run(
                        cypher_query, chunk_ids=chunk_ids, category=category
                    )

                    # Process results
                    related_docs = set()
                    for record in result:
                        doc_id = record["doc_id"]
                        title = record["title"]

                        # Add the document itself
                        results["related_documents"].append(
                            {"doc_id": doc_id, "title": title}
                        )

                        # Add related documents
                        for related in record["related_docs"]:
                            if related["doc_id"] not in related_docs:
                                related_docs.add(related["doc_id"])
                                results["related_documents"].append(
                                    {
                                        "doc_id": related["doc_id"],
                                        "title": related["title"],
                                    }
                                )

                                # Limit the number of related documents
                                if len(related_docs) >= limit:
                                    break

            except Exception as e:
                results["error"] = f"Neo4j query error: {e}"

        return results

    def hybrid_search(
        self,
        query: str,
        limit: int = 5,
        category: Optional[str] = None,
        expand_context: bool = True,
    ) -> Dict[str, Any]:
        """
        Perform hybrid search using both vector similarity and graph context.

        Args:
            query: Search query
            limit: Maximum results
            category: Optional category filter
            expand_context: Whether to expand results with graph context

        Returns:
            Combined search results
        """
        # Get vector search results
        vector_results = self.search_documentation(
            query, limit=limit * 2, category=category
        )

        # If we don't want expanded context, return vector results
        if not expand_context:
            return vector_results

        # Extract document IDs from vector results
        doc_ids = [doc["doc_id"] for doc in vector_results.get("related_documents", [])]

        # Expand with graph context
        try:
            with self.neo4j_driver.session() as session:
                cypher_query = """
                MATCH (d:Document)
                WHERE d.id IN $doc_ids
                OPTIONAL MATCH (d)-[:RELATED_TO*1..2]->(related:Document)
                WITH related, d
                WHERE related IS NOT NULL AND related.id NOT IN $doc_ids
                RETURN DISTINCT related.id as doc_id, related.title as title,
                       count(*) as relevance_score
                ORDER BY relevance_score DESC
                LIMIT $limit
                """

                result = session.run(cypher_query, doc_ids=doc_ids, limit=limit)

                # Add expanded results
                for record in result:
                    vector_results["related_documents"].append(
                        {
                            "doc_id": record["doc_id"],
                            "title": record["title"],
                            "graph_score": record["relevance_score"],
                        }
                    )
        except Exception as e:
            vector_results["error"] = f"Graph expansion error: {e}"

        return vector_results

#!/usr/bin/env python3
"""
Neo4j/Qdrant Operation Check Script

This script checks the following:
1. Direct access to Neo4j (Bolt protocol)
2. Direct access to Qdrant (HTTP API)
3. Access via GraphRAG MCP

Usage:
    # Test all (default)
    python scripts/check_graphrag_services.py

    # Test direct access only
    python scripts/check_graphrag_services.py --direct-only

    # Test MCP only
    python scripts/check_graphrag_services.py --mcp-only
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from auto_coder.logger_config import get_logger
from auto_coder.utils import is_running_in_container

logger = get_logger(__name__)


def run_docker_command(args: list[str]) -> subprocess.CompletedProcess:
    """Execute Docker command (use sudo if necessary)"""
    try:
        # Try without sudo first
        return subprocess.run(
            ["docker"] + args,
            capture_output=True,
            text=True,
            check=True
        )
    except (subprocess.CalledProcessError, PermissionError):
        # Retry with sudo if failed
        return subprocess.run(
            ["sudo", "docker"] + args,
            capture_output=True,
            text=True,
            check=True
        )


def get_current_container_network() -> str | None:
    """Get the network that the current container belongs to"""
    try:
        # Get hostname (container ID or container name)
        hostname = subprocess.run(
            ["hostname"],
            capture_output=True,
            text=True,
            check=True
        ).stdout.strip()

        # Get container network information
        result = run_docker_command([
            "inspect", "-f",
            "{{range $net, $conf := .NetworkSettings.Networks}}{{$net}}{{end}}",
            hostname
        ])
        network = result.stdout.strip()
        return network if network else None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def ensure_container_on_network(container_name: str, network: str) -> bool:
    """Ensure container is connected to the specified network, connect if necessary"""
    try:
        # Check if container is already connected to the network
        result = run_docker_command([
            "inspect", "-f",
            "{{range $net, $conf := .NetworkSettings.Networks}}{{$net}} {{end}}",
            container_name
        ])
        networks = result.stdout.strip().split()

        if network in networks:
            logger.info(f"✅ {container_name} is already connected to {network}")
            return True

        # Connect to network
        logger.info(f"🔗 Connecting {container_name} to {network}...")
        run_docker_command(["network", "connect", network, container_name])
        logger.info(f"✅ Connected {container_name} to {network}")
        return True

    except subprocess.CalledProcessError as e:
        logger.error(f"❌ Network connection error for {container_name}: {e.stderr}")
        return False


def check_neo4j_direct():
    """Test direct access to Neo4j"""
    logger.info("=" * 80)
    logger.info("Neo4j Direct Access Test")
    logger.info("=" * 80)

    try:
        from neo4j import GraphDatabase
    except ImportError:
        logger.error("neo4j package is not installed")
        logger.info("Install: pip install neo4j")
        return False

    # If running in a container, check network and connect
    in_container = is_running_in_container()
    if in_container:
        logger.info("🐳 Running in container")
        current_network = get_current_container_network()
        if current_network:
            logger.info(f"📡 Current network: {current_network}")
            # Connect Neo4j container to the same network
            if not ensure_container_on_network("auto-coder-neo4j", current_network):
                logger.warning("⚠️ Failed to connect Neo4j container to network")
        else:
            logger.warning("⚠️ Could not detect current network")

    # Try multiple URIs
    uris = []

    # Prioritize container name if in container
    if in_container:
        uris.append("bolt://auto-coder-neo4j:7687")

    # Also try normal localhost access
    uris.extend([
        "bolt://localhost:7687",
        "bolt://127.0.0.1:7687",
    ])

    # Also try container name if not in container
    if not in_container:
        uris.append("bolt://auto-coder-neo4j:7687")

    user = "neo4j"
    password = "password"

    driver = None
    last_error = None

    for uri in uris:
        logger.info(f"Connection attempt: {uri}")
        logger.info(f"User: {user}")

        try:
            driver = GraphDatabase.driver(uri, auth=(user, password), max_connection_lifetime=3600)
            # Connection test
            driver.verify_connectivity()
            logger.info(f"✅ Connection successful: {uri}")
            break
        except Exception as e:
            last_error = e
            logger.warning(f"Connection failed: {uri} - {e}")
            if driver:
                driver.close()
                driver = None

    if not driver:
        logger.error(f"❌ Neo4j connection error: Failed to connect to all URIs")
        logger.error(f"Last error: {last_error}")
        logger.info("\nTroubleshooting:")
        logger.info("1. Check if Docker container is running:")
        logger.info("   docker ps | grep neo4j")
        logger.info("2. Wait for Neo4j to start:")
        logger.info("   docker logs auto-coder-neo4j")
        logger.info("3. Test connection from within container:")
        logger.info("   docker exec auto-coder-neo4j cypher-shell -u neo4j -p password 'RETURN 1;'")
        return False

    try:

        with driver.session() as session:
            # 1. Check database version
            result = session.run("CALL dbms.components() YIELD name, versions RETURN name, versions")
            for record in result:
                logger.info(f"✅ Neo4j connection successful: {record['name']} {record['versions']}")

            # 2. Check existing node count
            result = session.run("MATCH (n) RETURN count(n) as count")
            count = result.single()["count"]
            logger.info(f"📊 Existing node count: {count}")

            # 3. Create sample node
            logger.info("\n--- Sample Node Creation Test ---")
            result = session.run(
                """
                CREATE (p:Person {name: $name, role: $role, created_at: datetime()})
                RETURN p
                """,
                name="Test User",
                role="Developer"
            )
            node = result.single()["p"]
            logger.info(f"✅ Node creation successful: {dict(node)}")

            # 4. Node search
            logger.info("\n--- Node Search Test ---")
            result = session.run(
                """
                MATCH (p:Person {name: $name})
                RETURN p
                """,
                name="Test User"
            )
            for record in result:
                logger.info(f"🔍 Search result: {dict(record['p'])}")

            # 5. Create relationship
            logger.info("\n--- Relationship Creation Test ---")
            result = session.run(
                """
                MATCH (p:Person {name: $name})
                CREATE (p)-[r:WORKS_ON]->(proj:Project {name: $project})
                RETURN p, r, proj
                """,
                name="Test User",
                project="GraphRAG Integration"
            )
            record = result.single()
            logger.info(f"✅ Relationship creation successful")
            logger.info(f"   Person: {dict(record['p'])}")
            logger.info(f"   Project: {dict(record['proj'])}")

            # 6. Path search
            logger.info("\n--- Path Search Test ---")
            result = session.run(
                """
                MATCH path = (p:Person)-[r:WORKS_ON]->(proj:Project)
                WHERE p.name = $name
                RETURN p.name as person, type(r) as relationship, proj.name as project
                """,
                name="Test User"
            )
            for record in result:
                logger.info(f"🔍 Path: {record['person']} -{record['relationship']}-> {record['project']}")

            # 7. Cleanup
            logger.info("\n--- Cleanup ---")
            session.run(
                """
                MATCH (p:Person {name: $name})
                DETACH DELETE p
                """,
                name="Test User"
            )
            logger.info("✅ Test data deletion completed")

        driver.close()
        logger.info("\n✅ Neo4j direct access test completed")
        return True

    except Exception as e:
        logger.error(f"❌ Neo4j test execution error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        if driver:
            driver.close()
        return False


def check_qdrant_direct(test_mode: bool = False):
    """Test direct access to Qdrant

    Args:
        test_mode: If True, create a collection for connection testing
    """
    logger.info("\n" + "=" * 80)
    logger.info("Qdrant Direct Access Test")
    logger.info("=" * 80)

    try:
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams, PointStruct
    except ImportError:
        logger.error("qdrant-client package is not installed")
        logger.info("Install: pip install qdrant-client")
        return False

    # If running in a container, check network and connect
    in_container = is_running_in_container()
    if in_container:
        logger.info("🐳 Running in container")
        current_network = get_current_container_network()
        if current_network:
            logger.info(f"📡 Current network: {current_network}")
            # Connect Qdrant container to the same network
            if not ensure_container_on_network("auto-coder-qdrant", current_network):
                logger.warning("⚠️ Failed to connect Qdrant container to network")
        else:
            logger.warning("⚠️ Could not detect current network")

    # Try multiple URLs
    urls = []

    # Prioritize container name if in container
    if in_container:
        urls.append("http://auto-coder-qdrant:6333")

    # Also try normal localhost access
    urls.extend([
        "http://localhost:6333",
        "http://127.0.0.1:6333",
    ])

    # Also try container name if not in container
    if not in_container:
        urls.append("http://auto-coder-qdrant:6333")

    client = None
    last_error = None

    for url in urls:
        logger.info(f"Connection attempt: {url}")

        try:
            test_client = QdrantClient(url=url, timeout=5)
            # Actually test the connection
            collections = test_client.get_collections()
            client = test_client
            logger.info(f"✅ Qdrant connection successful: {url}")
            break
        except Exception as e:
            last_error = e
            logger.warning(f"Connection failed: {url} - {e}")

    if not client:
        logger.error(f"❌ Qdrant connection error: Failed to connect to all URLs")
        logger.error(f"Last error: {last_error}")
        logger.info("\nTroubleshooting:")
        logger.info("1. Check if Docker container is running:")
        logger.info("   docker ps | grep qdrant")
        logger.info("2. Wait for Qdrant to start:")
        logger.info("   docker logs auto-coder-qdrant")
        logger.info("3. Test connection from within container:")
        logger.info("   docker exec auto-coder-qdrant wget -O- http://localhost:6333/collections")
        return False

    try:
        # 2. Collection list
        collections = client.get_collections()
        logger.info(f"📊 Existing collection count: {len(collections.collections)}")

        # Display existing collection information
        existing_collections = []
        for col in collections.collections:
            logger.info(f"   - {col.name}")
            existing_collections.append(col.name)

            # Display detailed information for existing collections
            try:
                col_info = client.get_collection(col.name)
                logger.info(f"     Vector count: {col_info.points_count}")
                if col_info.points_count > 0:
                    logger.info(f"     Vector dimensions: {col_info.config.params.vectors.size}")
                    logger.info(f"     Distance function: {col_info.config.params.vectors.distance}")
            except Exception as e:
                logger.warning(f"     Collection info retrieval error: {e}")

        # If code_embeddings collection exists, use it for testing
        if "code_embeddings" in existing_collections:
            logger.info("\n--- Existing Data Search Test (code_embeddings) ---")
            try:
                col_info = client.get_collection("code_embeddings")
                if col_info.points_count > 0:
                    # Get sample points
                    sample_points = client.scroll(
                        collection_name="code_embeddings",
                        limit=3,
                        with_payload=True,
                        with_vectors=False
                    )

                    logger.info(f"📊 Sample data (max 3 items):")
                    for point in sample_points[0]:
                        logger.info(f"  ID={point.id}")
                        logger.info(f"  Payload: {point.payload}")
                else:
                    logger.info("code_embeddings collection is empty")
            except Exception as e:
                logger.warning(f"Existing data search error: {e}")

        # 3. Create test collection (for connection testing) - only if test_mode
        if test_mode:
            collection_name = "test_collection"
            logger.info(f"\n--- Create Connection Test Collection: {collection_name} ---")

            # Delete existing test collection
            try:
                client.delete_collection(collection_name)
                logger.info(f"Deleted existing collection {collection_name}")
            except Exception:
                pass

            # Create new
            client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(size=4, distance=Distance.COSINE),
            )
            logger.info(f"✅ Collection creation successful: {collection_name}")

            # 4. Insert vectors (for connection testing)
            logger.info("\n--- Vector Insertion Test ---")
            points = [
                PointStruct(
                    id=1,
                    vector=[0.1, 0.2, 0.3, 0.4],
                    payload={"name": "Test Document 1", "type": "test"}
                ),
                PointStruct(
                    id=2,
                    vector=[0.2, 0.3, 0.4, 0.5],
                    payload={"name": "Test Document 2", "type": "test"}
                ),
            ]
            client.upsert(collection_name=collection_name, points=points)
            logger.info(f"✅ {len(points)} test vectors inserted successfully")

            # 5. Check collection information
            info = client.get_collection(collection_name)
            logger.info(f"📊 Test collection information:")
            logger.info(f"   Vector count: {info.points_count}")
            logger.info(f"   Vector dimensions: {info.config.params.vectors.size}")
            logger.info(f"   Distance function: {info.config.params.vectors.distance}")

            # 6. Similarity search test
            logger.info("\n--- Similarity Search Test ---")
            search_vector = [0.15, 0.25, 0.35, 0.45]
            search_results = client.search(
                collection_name=collection_name,
                query_vector=search_vector,
                limit=2
            )
            logger.info(f"🔍 Search vector: {search_vector}")
            logger.info(f"Search results (top {len(search_results)}):")
            for i, result in enumerate(search_results, 1):
                logger.info(f"  {i}. ID={result.id}, Score={result.score:.4f}")
                logger.info(f"     Payload: {result.payload}")

            # 7. Cleanup
            logger.info("\n--- Cleanup ---")
            client.delete_collection(collection_name)
            logger.info(f"✅ Test collection deletion completed: {collection_name}")

        logger.info("\n✅ Qdrant direct access test completed")
        return True

    except Exception as e:
        logger.error(f"❌ Qdrant test execution error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False


def check_graphrag_mcp():
    """Test access via GraphRAG MCP"""
    logger.info("\n" + "=" * 80)
    logger.info("GraphRAG MCP Access Test")
    logger.info("=" * 80)

    try:
        from auto_coder.graphrag_mcp_integration import GraphRAGMCPIntegration
        from auto_coder.graphrag_index_manager import GraphRAGIndexManager
    except ImportError as e:
        logger.error(f"GraphRAG MCP module import error: {e}")
        return False

    try:
        # Use current directory as target
        current_dir = Path.cwd()
        logger.info(f"Target directory: {current_dir}")

        # Check if current directory is empty
        is_empty = not any(current_dir.iterdir())

        # Initialize index manager with current directory
        index_manager = GraphRAGIndexManager(repo_path=str(current_dir))
        integration = GraphRAGMCPIntegration(index_manager=index_manager)

        # 1. Check Docker container status
        logger.info("\n--- Docker Container Status Check ---")
        status = integration.docker_manager.get_status()
        logger.info(f"Neo4j: {status['neo4j']}")
        logger.info(f"Qdrant: {status['qdrant']}")

        if status['neo4j'] != 'running' or status['qdrant'] != 'running':
            logger.warning("⚠️ Containers are not running. Attempting to start...")
            if not integration.docker_manager.start():
                logger.error("❌ Failed to start containers")
                return False

        # 2. Check MCP server status
        logger.info("\n--- MCP Server Status Check ---")
        if integration.is_mcp_server_running():
            logger.info("✅ MCP server is running")
        else:
            logger.warning("⚠️ MCP server is not running")
            logger.info("How to start MCP server:")
            logger.info("  cd ~/graphrag_mcp && uv run main.py")

        # 3. Check and update index status
        logger.info("\n--- Index Status Check ---")

        # Create sample data if current directory is empty
        if is_empty:
            logger.info("📝 Current directory is empty. Creating sample data...")
            sample_file = current_dir / "sample.py"
            sample_file.write_text("""# Sample Python file for GraphRAG indexing test
def hello_world():
    \"\"\"A simple hello world function.\"\"\"
    print("Hello, World!")

class SampleClass:
    \"\"\"A sample class for testing.\"\"\"
    def __init__(self, name: str):
        self.name = name

    def greet(self):
        \"\"\"Greet with the name.\"\"\"
        return f"Hello, {self.name}!"
""")
            logger.info(f"✅ Created sample file: {sample_file}")

        # Check if collections exist
        has_collections = False
        try:
            from qdrant_client import QdrantClient

            # Use container name if running in container
            in_container = is_running_in_container()
            qdrant_url = "http://auto-coder-qdrant:6333" if in_container else "http://localhost:6333"

            # Connect to Qdrant
            qdrant_client = QdrantClient(url=qdrant_url, timeout=5)

            # Get existing collections
            collections = qdrant_client.get_collections()
            has_collections = len(collections.collections) > 0
        except Exception as e:
            logger.warning(f"Qdrant connection error: {e}")

        if has_collections:
            if integration.index_manager.is_index_up_to_date():
                logger.info("✅ Index is up to date")
            else:
                logger.warning("⚠️ Index may be outdated")
                _, indexed_path = integration.index_manager.check_indexed_path()
                if indexed_path:
                    logger.info(f"Indexed path: {indexed_path}")
                logger.info(f"Current path: {integration.index_manager.repo_path}")

                # Update index
                logger.info("🔄 Updating index...")
                if integration.index_manager.update_index(force=True):
                    logger.info("✅ Index updated")
                else:
                    logger.error("❌ Failed to update index")
                    return False
        else:
            logger.warning("⚠️ Collections do not exist")
            logger.info(f"Target directory: {integration.index_manager.repo_path}")

            # Create index
            logger.info("🔄 Creating index...")
            if integration.index_manager.update_index(force=True):
                logger.info("✅ Index created")
            else:
                logger.error("❌ Failed to create index")
                return False

        # 4. Check index data
        logger.info("\n--- Index Data Check ---")
        try:
            from qdrant_client import QdrantClient

            # Use container name if running in container
            in_container = is_running_in_container()
            qdrant_url = "http://auto-coder-qdrant:6333" if in_container else "http://localhost:6333"

            # Connect to Qdrant
            qdrant_client = QdrantClient(url=qdrant_url, timeout=5)

            # Get existing collections
            collections = qdrant_client.get_collections()
            logger.info(f"📊 Existing collection count: {len(collections.collections)}")

            if len(collections.collections) == 0:
                logger.info("   No collections exist (index not created)")
            else:
                for col in collections.collections:
                    logger.info(f"\n📦 Collection: {col.name}")
                    try:
                        col_info = qdrant_client.get_collection(col.name)
                        logger.info(f"   Vector count: {col_info.points_count}")

                        if col_info.points_count > 0:
                            # Get sample data
                            sample_points = qdrant_client.scroll(
                                collection_name=col.name,
                                limit=5,
                                with_payload=True,
                                with_vectors=False
                            )

                            logger.info(f"   Sample data (max 5 items):")
                            for point in sample_points[0]:
                                # Display payload content
                                payload_str = str(point.payload)
                                if len(payload_str) > 100:
                                    payload_str = payload_str[:100] + "..."
                                logger.info(f"     ID={point.id}: {payload_str}")
                    except Exception as e:
                        logger.warning(f"   Collection info retrieval error: {e}")

        except Exception as e:
            logger.warning(f"Qdrant connection error: {e}")

        # 5. Get MCP configuration
        logger.info("\n--- MCP Configuration ---")

        # Check if MCP server is running
        is_mcp_running = integration.is_mcp_server_running()
        if is_mcp_running:
            logger.info("✅ MCP server: Running")
            mcp_config = integration.get_mcp_config_for_llm()
            if mcp_config:
                logger.info("MCP configuration:")
                logger.info(json.dumps(mcp_config, indent=2, ensure_ascii=False))
        else:
            logger.info("ℹ️ MCP server: Not running")
            logger.info("   (MCP server does not start in --mcp-only mode)")
            logger.info("   Example MCP configuration:")
            example_config = {
                "mcp_server": "graphrag",
                "mcp_resources": [
                    "https://graphrag.db/schema/neo4j",
                    "https://graphrag.db/collection/qdrant",
                ],
                "note": "Tools are provided dynamically by MCP server: search_documentation, hybrid_search",
            }
            logger.info(json.dumps(example_config, indent=2, ensure_ascii=False))

        logger.info("\n✅ GraphRAG MCP test completed")
        return True

    except Exception as e:
        logger.error(f"❌ GraphRAG MCP error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Neo4j/Qdrant Operation Check Script",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Test all (default)
  python scripts/check_graphrag_services.py

  # Test direct access only
  python scripts/check_graphrag_services.py --direct-only

  # Test MCP only
  python scripts/check_graphrag_services.py --mcp-only

  # Test by creating a connection test collection
  python scripts/check_graphrag_services.py --test
        """
    )
    parser.add_argument(
        "--direct-only",
        action="store_true",
        help="Run only direct access tests (Neo4j + Qdrant)"
    )
    parser.add_argument(
        "--mcp-only",
        action="store_true",
        help="Run only GraphRAG MCP tests"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Create and test with a connection test collection"
    )

    args = parser.parse_args()

    results = {}

    # Default: test all
    # --direct-only: direct access only
    # --mcp-only: MCP only
    run_direct = not args.mcp_only
    run_mcp = not args.direct_only

    if run_direct:
        results["neo4j"] = check_neo4j_direct()
        results["qdrant"] = check_qdrant_direct(test_mode=args.test)

    if run_mcp:
        results["graphrag_mcp"] = check_graphrag_mcp()

    # Summary
    logger.info("\n" + "=" * 80)
    logger.info("Test Result Summary")
    logger.info("=" * 80)
    for name, result in results.items():
        status = "✅ Success" if result else "❌ Failed"
        logger.info(f"{name}: {status}")

    # Exit code
    all_passed = all(results.values())
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()

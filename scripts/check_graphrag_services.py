#!/usr/bin/env python3
"""
Neo4j/Qdrant 動作確認スクリプト

このスクリプトは以下を確認します:
1. Neo4j への直接アクセス（Bolt プロトコル）
2. Qdrant への直接アクセス（HTTP API）
3. GraphRAG MCP 経由でのアクセス

使用方法:
    # 全部テスト（デフォルト）
    python scripts/check_graphrag_services.py

    # 直接アクセスのみテスト
    python scripts/check_graphrag_services.py --direct-only

    # MCP のみテスト
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

logger = get_logger(__name__)


def is_running_in_container() -> bool:
    """コンテナ内で実行されているかを判定"""
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def run_docker_command(args: list[str]) -> subprocess.CompletedProcess:
    """Dockerコマンドを実行（必要に応じてsudoを使用）"""
    try:
        # まずsudoなしで試行
        return subprocess.run(
            ["docker"] + args, capture_output=True, text=True, check=True
        )
    except (subprocess.CalledProcessError, PermissionError):
        # 失敗したらsudoで試行
        return subprocess.run(
            ["sudo", "docker"] + args, capture_output=True, text=True, check=True
        )


def get_current_container_network() -> str | None:
    """現在のコンテナが所属しているネットワークを取得"""
    try:
        # ホスト名を取得（コンテナIDまたはコンテナ名）
        hostname = subprocess.run(
            ["hostname"], capture_output=True, text=True, check=True
        ).stdout.strip()

        # コンテナのネットワーク情報を取得
        result = run_docker_command(
            [
                "inspect",
                "-f",
                "{{range $net, $conf := .NetworkSettings.Networks}}{{$net}}{{end}}",
                hostname,
            ]
        )
        network = result.stdout.strip()
        return network if network else None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def ensure_container_on_network(container_name: str, network: str) -> bool:
    """コンテナが指定されたネットワークに接続されていることを確認し、必要に応じて接続"""
    try:
        # コンテナが既にネットワークに接続されているか確認
        result = run_docker_command(
            [
                "inspect",
                "-f",
                "{{range $net, $conf := .NetworkSettings.Networks}}{{$net}} {{end}}",
                container_name,
            ]
        )
        networks = result.stdout.strip().split()

        if network in networks:
            logger.info(f"✅ {container_name} は既に {network} に接続されています")
            return True

        # ネットワークに接続
        logger.info(f"🔗 {container_name} を {network} に接続中...")
        run_docker_command(["network", "connect", network, container_name])
        logger.info(f"✅ {container_name} を {network} に接続しました")
        return True

    except subprocess.CalledProcessError as e:
        logger.error(f"❌ {container_name} のネットワーク接続エラー: {e.stderr}")
        return False


def check_neo4j_direct():
    """Neo4j への直接アクセスをテスト"""
    logger.info("=" * 80)
    logger.info("Neo4j 直接アクセステスト")
    logger.info("=" * 80)

    try:
        from neo4j import GraphDatabase
    except ImportError:
        logger.error("neo4j パッケージがインストールされていません")
        logger.info("インストール: pip install neo4j")
        return False

    # コンテナ内で実行されている場合、ネットワークを確認して接続
    in_container = is_running_in_container()
    if in_container:
        logger.info("🐳 コンテナ内で実行されています")
        current_network = get_current_container_network()
        if current_network:
            logger.info(f"📡 現在のネットワーク: {current_network}")
            # Neo4jコンテナを同じネットワークに接続
            if not ensure_container_on_network("auto-coder-neo4j", current_network):
                logger.warning("⚠️  Neo4jコンテナのネットワーク接続に失敗しました")
        else:
            logger.warning("⚠️  現在のネットワークを検出できませんでした")

    # 複数のURIを試行
    uris = []

    # コンテナ内の場合はコンテナ名を優先
    if in_container:
        uris.append("bolt://auto-coder-neo4j:7687")

    # 通常のlocalhostアクセスも試行
    uris.extend(
        [
            "bolt://localhost:7687",
            "bolt://127.0.0.1:7687",
        ]
    )

    # コンテナ外の場合もコンテナ名を試行
    if not in_container:
        uris.append("bolt://auto-coder-neo4j:7687")

    user = "neo4j"
    password = "password"

    driver = None
    last_error = None

    for uri in uris:
        logger.info(f"接続試行: {uri}")
        logger.info(f"ユーザー: {user}")

        try:
            driver = GraphDatabase.driver(
                uri, auth=(user, password), max_connection_lifetime=3600
            )
            # 接続テスト
            driver.verify_connectivity()
            logger.info(f"✅ 接続成功: {uri}")
            break
        except Exception as e:
            last_error = e
            logger.warning(f"接続失敗: {uri} - {e}")
            if driver:
                driver.close()
                driver = None

    if not driver:
        logger.error(f"❌ Neo4j 接続エラー: すべてのURIで接続失敗")
        logger.error(f"最後のエラー: {last_error}")
        logger.info("\nトラブルシューティング:")
        logger.info("1. Docker コンテナが起動しているか確認:")
        logger.info("   docker ps | grep neo4j")
        logger.info("2. Neo4j が起動するまで待つ:")
        logger.info("   docker logs auto-coder-neo4j")
        logger.info("3. コンテナ内から接続テスト:")
        logger.info(
            "   docker exec auto-coder-neo4j cypher-shell -u neo4j -p password 'RETURN 1;'"
        )
        return False

    try:

        with driver.session() as session:
            # 1. データベースバージョン確認
            result = session.run(
                "CALL dbms.components() YIELD name, versions RETURN name, versions"
            )
            for record in result:
                logger.info(f"✅ Neo4j 接続成功: {record['name']} {record['versions']}")

            # 2. 既存ノード数確認
            result = session.run("MATCH (n) RETURN count(n) as count")
            count = result.single()["count"]
            logger.info(f"📊 既存ノード数: {count}")

            # 3. サンプルノード作成
            logger.info("\n--- サンプルノード作成テスト ---")
            result = session.run(
                """
                CREATE (p:Person {name: $name, role: $role, created_at: datetime()})
                RETURN p
                """,
                name="Test User",
                role="Developer",
            )
            node = result.single()["p"]
            logger.info(f"✅ ノード作成成功: {dict(node)}")

            # 4. ノード検索
            logger.info("\n--- ノード検索テスト ---")
            result = session.run(
                """
                MATCH (p:Person {name: $name})
                RETURN p
                """,
                name="Test User",
            )
            for record in result:
                logger.info(f"🔍 検索結果: {dict(record['p'])}")

            # 5. リレーションシップ作成
            logger.info("\n--- リレーションシップ作成テスト ---")
            result = session.run(
                """
                MATCH (p:Person {name: $name})
                CREATE (p)-[r:WORKS_ON]->(proj:Project {name: $project})
                RETURN p, r, proj
                """,
                name="Test User",
                project="GraphRAG Integration",
            )
            record = result.single()
            logger.info(f"✅ リレーションシップ作成成功")
            logger.info(f"   Person: {dict(record['p'])}")
            logger.info(f"   Project: {dict(record['proj'])}")

            # 6. パス検索
            logger.info("\n--- パス検索テスト ---")
            result = session.run(
                """
                MATCH path = (p:Person)-[r:WORKS_ON]->(proj:Project)
                WHERE p.name = $name
                RETURN p.name as person, type(r) as relationship, proj.name as project
                """,
                name="Test User",
            )
            for record in result:
                logger.info(
                    f"🔍 パス: {record['person']} -{record['relationship']}-> {record['project']}"
                )

            # 7. クリーンアップ
            logger.info("\n--- クリーンアップ ---")
            session.run(
                """
                MATCH (p:Person {name: $name})
                DETACH DELETE p
                """,
                name="Test User",
            )
            logger.info("✅ テストデータ削除完了")

        driver.close()
        logger.info("\n✅ Neo4j 直接アクセステスト完了")
        return True

    except Exception as e:
        logger.error(f"❌ Neo4j テスト実行エラー: {e}")
        import traceback

        logger.error(traceback.format_exc())
        if driver:
            driver.close()
        return False


def check_qdrant_direct(test_mode: bool = False):
    """Qdrant への直接アクセスをテスト

    Args:
        test_mode: Trueの場合、接続テスト用コレクションを作成
    """
    logger.info("\n" + "=" * 80)
    logger.info("Qdrant 直接アクセステスト")
    logger.info("=" * 80)

    try:
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, PointStruct, VectorParams
    except ImportError:
        logger.error("qdrant-client パッケージがインストールされていません")
        logger.info("インストール: pip install qdrant-client")
        return False

    # コンテナ内で実行されている場合、ネットワークを確認して接続
    in_container = is_running_in_container()
    if in_container:
        logger.info("🐳 コンテナ内で実行されています")
        current_network = get_current_container_network()
        if current_network:
            logger.info(f"📡 現在のネットワーク: {current_network}")
            # Qdrantコンテナを同じネットワークに接続
            if not ensure_container_on_network("auto-coder-qdrant", current_network):
                logger.warning("⚠️  Qdrantコンテナのネットワーク接続に失敗しました")
        else:
            logger.warning("⚠️  現在のネットワークを検出できませんでした")

    # 複数のURLを試行
    urls = []

    # コンテナ内の場合はコンテナ名を優先
    if in_container:
        urls.append("http://auto-coder-qdrant:6333")

    # 通常のlocalhostアクセスも試行
    urls.extend(
        [
            "http://localhost:6333",
            "http://127.0.0.1:6333",
        ]
    )

    # コンテナ外の場合もコンテナ名を試行
    if not in_container:
        urls.append("http://auto-coder-qdrant:6333")

    client = None
    last_error = None

    for url in urls:
        logger.info(f"接続試行: {url}")

        try:
            test_client = QdrantClient(url=url, timeout=5)
            # 実際に接続テスト
            collections = test_client.get_collections()
            client = test_client
            logger.info(f"✅ Qdrant 接続成功: {url}")
            break
        except Exception as e:
            last_error = e
            logger.warning(f"接続失敗: {url} - {e}")

    if not client:
        logger.error(f"❌ Qdrant 接続エラー: すべてのURLで接続失敗")
        logger.error(f"最後のエラー: {last_error}")
        logger.info("\nトラブルシューティング:")
        logger.info("1. Docker コンテナが起動しているか確認:")
        logger.info("   docker ps | grep qdrant")
        logger.info("2. Qdrant が起動するまで待つ:")
        logger.info("   docker logs auto-coder-qdrant")
        logger.info("3. コンテナ内から接続テスト:")
        logger.info(
            "   docker exec auto-coder-qdrant wget -O- http://localhost:6333/collections"
        )
        return False

    try:
        # 2. コレクション一覧
        collections = client.get_collections()
        logger.info(f"📊 既存コレクション数: {len(collections.collections)}")

        # 既存のコレクション情報を表示
        existing_collections = []
        for col in collections.collections:
            logger.info(f"   - {col.name}")
            existing_collections.append(col.name)

            # 既存コレクションの詳細情報を表示
            try:
                col_info = client.get_collection(col.name)
                logger.info(f"     ベクトル数: {col_info.points_count}")
                if col_info.points_count > 0:
                    logger.info(
                        f"     ベクトル次元: {col_info.config.params.vectors.size}"
                    )
                    logger.info(
                        f"     距離関数: {col_info.config.params.vectors.distance}"
                    )
            except Exception as e:
                logger.warning(f"     コレクション情報取得エラー: {e}")

        # 既存のcode_embeddingsコレクションがあれば、それを使用してテスト
        if "code_embeddings" in existing_collections:
            logger.info("\n--- 既存データ検索テスト (code_embeddings) ---")
            try:
                col_info = client.get_collection("code_embeddings")
                if col_info.points_count > 0:
                    # サンプルポイントを取得
                    sample_points = client.scroll(
                        collection_name="code_embeddings",
                        limit=3,
                        with_payload=True,
                        with_vectors=False,
                    )

                    logger.info(f"📊 サンプルデータ (最大3件):")
                    for point in sample_points[0]:
                        logger.info(f"  ID={point.id}")
                        logger.info(f"  Payload: {point.payload}")
                else:
                    logger.info("code_embeddingsコレクションは空です")
            except Exception as e:
                logger.warning(f"既存データ検索エラー: {e}")

        # 3. テストコレクション作成（接続テスト用）- test_modeの場合のみ
        if test_mode:
            collection_name = "test_collection"
            logger.info(f"\n--- 接続テスト用コレクション作成: {collection_name} ---")

            # 既存のテストコレクションを削除
            try:
                client.delete_collection(collection_name)
                logger.info(f"既存のコレクション {collection_name} を削除")
            except Exception:
                pass

            # 新規作成
            client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(size=4, distance=Distance.COSINE),
            )
            logger.info(f"✅ コレクション作成成功: {collection_name}")

            # 4. ベクトル挿入（接続テスト用）
            logger.info("\n--- ベクトル挿入テスト ---")
            points = [
                PointStruct(
                    id=1,
                    vector=[0.1, 0.2, 0.3, 0.4],
                    payload={"name": "Test Document 1", "type": "test"},
                ),
                PointStruct(
                    id=2,
                    vector=[0.2, 0.3, 0.4, 0.5],
                    payload={"name": "Test Document 2", "type": "test"},
                ),
            ]
            client.upsert(collection_name=collection_name, points=points)
            logger.info(f"✅ {len(points)} 件のテストベクトル挿入成功")

            # 5. コレクション情報確認
            info = client.get_collection(collection_name)
            logger.info(f"📊 テストコレクション情報:")
            logger.info(f"   ベクトル数: {info.points_count}")
            logger.info(f"   ベクトル次元: {info.config.params.vectors.size}")
            logger.info(f"   距離関数: {info.config.params.vectors.distance}")

            # 6. 類似検索テスト
            logger.info("\n--- 類似検索テスト ---")
            search_vector = [0.15, 0.25, 0.35, 0.45]
            search_results = client.search(
                collection_name=collection_name, query_vector=search_vector, limit=2
            )
            logger.info(f"🔍 検索ベクトル: {search_vector}")
            logger.info(f"検索結果 (上位 {len(search_results)} 件):")
            for i, result in enumerate(search_results, 1):
                logger.info(f"  {i}. ID={result.id}, Score={result.score:.4f}")
                logger.info(f"     Payload: {result.payload}")

            # 7. クリーンアップ
            logger.info("\n--- クリーンアップ ---")
            client.delete_collection(collection_name)
            logger.info(f"✅ テストコレクション削除完了: {collection_name}")

        logger.info("\n✅ Qdrant 直接アクセステスト完了")
        return True

    except Exception as e:
        logger.error(f"❌ Qdrant テスト実行エラー: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return False


def check_graphrag_mcp():
    """GraphRAG MCP 経由でのアクセスをテスト"""
    logger.info("\n" + "=" * 80)
    logger.info("GraphRAG MCP 経由アクセステスト")
    logger.info("=" * 80)

    try:
        from auto_coder.graphrag_index_manager import GraphRAGIndexManager
        from auto_coder.graphrag_mcp_integration import GraphRAGMCPIntegration
    except ImportError as e:
        logger.error(f"GraphRAG MCP モジュールのインポートエラー: {e}")
        return False

    try:
        # カレントディレクトリを対象にする
        current_dir = Path.cwd()
        logger.info(f"対象ディレクトリ: {current_dir}")

        # カレントディレクトリが空かどうかをチェック
        is_empty = not any(current_dir.iterdir())

        # インデックスマネージャーをカレントディレクトリで初期化
        index_manager = GraphRAGIndexManager(repo_path=str(current_dir))
        integration = GraphRAGMCPIntegration(index_manager=index_manager)

        # 1. Docker コンテナ状態確認
        logger.info("\n--- Docker コンテナ状態確認 ---")
        status = integration.docker_manager.get_status()
        logger.info(f"Neo4j: {status['neo4j']}")
        logger.info(f"Qdrant: {status['qdrant']}")

        if status["neo4j"] != "running" or status["qdrant"] != "running":
            logger.warning("⚠️  コンテナが起動していません。起動を試みます...")
            if not integration.docker_manager.start():
                logger.error("❌ コンテナの起動に失敗しました")
                return False

        # 2. MCP サーバー状態確認
        logger.info("\n--- MCP サーバー状態確認 ---")
        if integration.is_mcp_server_running():
            logger.info("✅ MCP サーバーは起動しています")
        else:
            logger.warning("⚠️  MCP サーバーが起動していません")
            logger.info("MCP サーバーの起動方法:")
            logger.info("  cd ~/graphrag_mcp && uv run main.py")

        # 3. インデックス状態確認と更新
        logger.info("\n--- インデックス状態確認 ---")

        # カレントディレクトリが空の場合はサンプルデータを作成
        if is_empty:
            logger.info(
                "📝 カレントディレクトリが空です。サンプルデータを作成します..."
            )
            sample_file = current_dir / "sample.py"
            sample_file.write_text(
                """# Sample Python file for GraphRAG indexing test
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
"""
            )
            logger.info(f"✅ サンプルファイルを作成しました: {sample_file}")

        # コレクションが存在するかチェック
        has_collections = False
        try:
            from qdrant_client import QdrantClient

            # コンテナ内で実行されている場合はコンテナ名を使用
            in_container = is_running_in_container()
            qdrant_url = (
                "http://auto-coder-qdrant:6333"
                if in_container
                else "http://localhost:6333"
            )

            # Qdrantに接続
            qdrant_client = QdrantClient(url=qdrant_url, timeout=5)

            # 既存のコレクション一覧を取得
            collections = qdrant_client.get_collections()
            has_collections = len(collections.collections) > 0
        except Exception as e:
            logger.warning(f"Qdrant接続エラー: {e}")

        if has_collections:
            if integration.index_manager.is_index_up_to_date():
                logger.info("✅ インデックスは最新です")
            else:
                logger.warning("⚠️  インデックスが古い可能性があります")
                _, indexed_path = integration.index_manager.check_indexed_path()
                if indexed_path:
                    logger.info(f"インデックス済みパス: {indexed_path}")
                logger.info(f"現在のパス: {integration.index_manager.repo_path}")

                # インデックスを更新
                logger.info("🔄 インデックスを更新しています...")
                if integration.index_manager.update_index(force=True):
                    logger.info("✅ インデックスを更新しました")
                else:
                    logger.error("❌ インデックスの更新に失敗しました")
                    return False
        else:
            logger.warning("⚠️  コレクションが存在しません")
            logger.info(f"対象ディレクトリ: {integration.index_manager.repo_path}")

            # インデックスを作成
            logger.info("🔄 インデックスを作成しています...")
            if integration.index_manager.update_index(force=True):
                logger.info("✅ インデックスを作成しました")
            else:
                logger.error("❌ インデックスの作成に失敗しました")
                return False

        # 4. インデックスデータの確認
        logger.info("\n--- インデックスデータ確認 ---")
        try:
            from qdrant_client import QdrantClient

            # コンテナ内で実行されている場合はコンテナ名を使用
            in_container = is_running_in_container()
            qdrant_url = (
                "http://auto-coder-qdrant:6333"
                if in_container
                else "http://localhost:6333"
            )

            # Qdrantに接続
            qdrant_client = QdrantClient(url=qdrant_url, timeout=5)

            # 既存のコレクション一覧を取得
            collections = qdrant_client.get_collections()
            logger.info(f"📊 既存コレクション数: {len(collections.collections)}")

            if len(collections.collections) == 0:
                logger.info("   コレクションが存在しません（インデックスが未作成）")
            else:
                for col in collections.collections:
                    logger.info(f"\n📦 コレクション: {col.name}")
                    try:
                        col_info = qdrant_client.get_collection(col.name)
                        logger.info(f"   ベクトル数: {col_info.points_count}")

                        if col_info.points_count > 0:
                            # サンプルデータを取得
                            sample_points = qdrant_client.scroll(
                                collection_name=col.name,
                                limit=5,
                                with_payload=True,
                                with_vectors=False,
                            )

                            logger.info(f"   サンプルデータ (最大5件):")
                            for point in sample_points[0]:
                                # ペイロードの内容を表示
                                payload_str = str(point.payload)
                                if len(payload_str) > 100:
                                    payload_str = payload_str[:100] + "..."
                                logger.info(f"     ID={point.id}: {payload_str}")
                    except Exception as e:
                        logger.warning(f"   コレクション情報取得エラー: {e}")

        except Exception as e:
            logger.warning(f"Qdrant接続エラー: {e}")

        # 5. MCP 設定取得
        logger.info("\n--- MCP 設定 ---")

        # MCPサーバーの起動状態を確認
        is_mcp_running = integration.is_mcp_server_running()
        if is_mcp_running:
            logger.info("✅ MCP サーバー: 起動中")
            mcp_config = integration.get_mcp_config_for_llm()
            if mcp_config:
                logger.info("MCP 設定:")
                logger.info(json.dumps(mcp_config, indent=2, ensure_ascii=False))
        else:
            logger.info("ℹ️  MCP サーバー: 未起動")
            logger.info("   (--mcp-only モードではMCPサーバーは起動しません)")
            logger.info("   MCP設定の例:")
            example_config = {
                "mcp_server": "graphrag",
                "mcp_resources": [
                    "https://graphrag.db/schema/neo4j",
                    "https://graphrag.db/collection/qdrant",
                ],
                "note": "Tools are provided dynamically by MCP server: search_documentation, hybrid_search",
            }
            logger.info(json.dumps(example_config, indent=2, ensure_ascii=False))

        logger.info("\n✅ GraphRAG MCP テスト完了")
        return True

    except Exception as e:
        logger.error(f"❌ GraphRAG MCP エラー: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Neo4j/Qdrant 動作確認スクリプト",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  # 全部テスト（デフォルト）
  python scripts/check_graphrag_services.py

  # 直接アクセスのみテスト
  python scripts/check_graphrag_services.py --direct-only

  # MCP のみテスト
  python scripts/check_graphrag_services.py --mcp-only

  # 接続テスト用コレクションを作成してテスト
  python scripts/check_graphrag_services.py --test
        """,
    )
    parser.add_argument(
        "--direct-only",
        action="store_true",
        help="直接アクセス（Neo4j + Qdrant）のテストのみ実行",
    )
    parser.add_argument(
        "--mcp-only", action="store_true", help="GraphRAG MCP のテストのみ実行"
    )
    parser.add_argument(
        "--test", action="store_true", help="接続テスト用コレクションを作成してテスト"
    )

    args = parser.parse_args()

    results = {}

    # デフォルト: 全部テスト
    # --direct-only: 直接アクセスのみ
    # --mcp-only: MCPのみ
    run_direct = not args.mcp_only
    run_mcp = not args.direct_only

    if run_direct:
        results["neo4j"] = check_neo4j_direct()
        results["qdrant"] = check_qdrant_direct(test_mode=args.test)

    if run_mcp:
        results["graphrag_mcp"] = check_graphrag_mcp()

    # サマリー
    logger.info("\n" + "=" * 80)
    logger.info("テスト結果サマリー")
    logger.info("=" * 80)
    for name, result in results.items():
        status = "✅ 成功" if result else "❌ 失敗"
        logger.info(f"{name}: {status}")

    # 終了コード
    all_passed = all(results.values())
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()

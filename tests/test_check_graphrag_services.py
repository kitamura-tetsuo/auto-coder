"""
scripts/check_graphrag_services.py のテスト
"""

import subprocess
import sys
from pathlib import Path

import pytest


def get_python_executable():
    """適切なPython実行ファイルを取得"""
    # venv内のPythonを優先
    venv_python = Path(__file__).parent.parent / "venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def test_check_graphrag_services_script_exists():
    """スクリプトファイルが存在することを確認"""
    script_path = Path(__file__).parent.parent / "scripts" / "check_graphrag_services.py"
    assert script_path.exists(), f"スクリプトが存在しません: {script_path}"
    assert script_path.is_file(), f"スクリプトがファイルではありません: {script_path}"


def test_check_graphrag_services_runs_successfully():
    """スクリプトが正常に実行されることを確認（デフォルト: 全部テスト）"""
    script_path = Path(__file__).parent.parent / "scripts" / "check_graphrag_services.py"
    python_exe = get_python_executable()

    # スクリプトを実行（デフォルトで全部テスト）
    result = subprocess.run(
        [python_exe, str(script_path)],
        capture_output=True,
        text=True,
        timeout=120
    )

    # 終了コードが0であることを確認
    assert result.returncode == 0, f"スクリプトが失敗しました:\nstdout: {result.stdout}\nstderr: {result.stderr}"

    # 出力に成功メッセージが含まれることを確認（全部テスト）
    assert "✅ Neo4j 直接アクセステスト完了" in result.stdout, "Neo4jテストの成功メッセージが見つかりません"
    assert "✅ Qdrant 直接アクセステスト完了" in result.stdout, "Qdrantテストの成功メッセージが見つかりません"
    assert "✅ GraphRAG MCP テスト完了" in result.stdout, "GraphRAG MCPテストの成功メッセージが見つかりません"
    assert "neo4j: ✅ 成功" in result.stdout, "Neo4jの最終結果が成功ではありません"
    assert "qdrant: ✅ 成功" in result.stdout, "Qdrantの最終結果が成功ではありません"
    assert "graphrag_mcp: ✅ 成功" in result.stdout, "GraphRAG MCPの最終結果が成功ではありません"


def test_check_graphrag_services_detects_container():
    """スクリプトがコンテナ内で実行されていることを検出することを確認"""
    script_path = Path(__file__).parent.parent / "scripts" / "check_graphrag_services.py"
    python_exe = get_python_executable()

    # スクリプトを実行
    result = subprocess.run(
        [python_exe, str(script_path)],
        capture_output=True,
        text=True,
        timeout=120
    )
    
    # コンテナ内で実行されている場合、対応するメッセージが出力されることを確認
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        assert "🐳 コンテナ内で実行されています" in result.stdout, "コンテナ検出メッセージが見つかりません"
        assert "📡 現在のネットワーク:" in result.stdout, "ネットワーク検出メッセージが見つかりません"


def test_check_graphrag_services_neo4j_operations():
    """Neo4jの各種操作が正常に実行されることを確認"""
    script_path = Path(__file__).parent.parent / "scripts" / "check_graphrag_services.py"
    python_exe = get_python_executable()

    # スクリプトを実行
    result = subprocess.run(
        [python_exe, str(script_path)],
        capture_output=True,
        text=True,
        timeout=120
    )
    
    # Neo4jの各種操作が実行されたことを確認
    assert "✅ 接続成功: bolt://auto-coder-neo4j:7687" in result.stdout, "Neo4j接続成功メッセージが見つかりません"
    assert "✅ ノード作成成功:" in result.stdout, "ノード作成成功メッセージが見つかりません"
    assert "🔍 検索結果:" in result.stdout, "検索結果メッセージが見つかりません"
    assert "✅ リレーションシップ作成成功" in result.stdout, "リレーションシップ作成成功メッセージが見つかりません"
    assert "🔍 パス:" in result.stdout, "パス検索結果メッセージが見つかりません"
    assert "✅ テストデータ削除完了" in result.stdout, "クリーンアップ成功メッセージが見つかりません"


def test_check_graphrag_services_qdrant_operations():
    """Qdrantの各種操作が正常に実行されることを確認"""
    script_path = Path(__file__).parent.parent / "scripts" / "check_graphrag_services.py"
    python_exe = get_python_executable()

    # スクリプトを--testオプション付きで実行
    result = subprocess.run(
        [python_exe, str(script_path), "--test"],
        capture_output=True,
        text=True,
        timeout=120
    )

    # Qdrantの各種操作が実行されたことを確認
    assert "✅ Qdrant 接続成功: http://auto-coder-qdrant:6333" in result.stdout, "Qdrant接続成功メッセージが見つかりません"
    assert "✅ コレクション作成成功: test_collection" in result.stdout, "コレクション作成成功メッセージが見つかりません"
    assert "✅ 2 件のテストベクトル挿入成功" in result.stdout, "ベクトル挿入成功メッセージが見つかりません"
    assert "📊 テストコレクション情報:" in result.stdout, "コレクション情報メッセージが見つかりません"
    assert "🔍 検索ベクトル:" in result.stdout, "検索ベクトルメッセージが見つかりません"
    assert "✅ テストコレクション削除完了: test_collection" in result.stdout, "クリーンアップ成功メッセージが見つかりません"


def test_check_graphrag_services_network_connection():
    """コンテナがネットワークに接続されることを確認"""
    script_path = Path(__file__).parent.parent / "scripts" / "check_graphrag_services.py"
    python_exe = get_python_executable()

    # スクリプトを実行
    result = subprocess.run(
        [python_exe, str(script_path)],
        capture_output=True,
        text=True,
        timeout=120
    )

    # コンテナ内で実行されている場合、ネットワーク接続メッセージが出力されることを確認
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        # ネットワーク接続または既に接続されているメッセージのいずれかが出力されることを確認
        network_connected = (
            "✅ auto-coder-neo4j を" in result.stdout and "に接続しました" in result.stdout
        ) or (
            "✅ auto-coder-neo4j は既に" in result.stdout and "に接続されています" in result.stdout
        )
        assert network_connected, "Neo4jコンテナのネットワーク接続メッセージが見つかりません"

        network_connected = (
            "✅ auto-coder-qdrant を" in result.stdout and "に接続しました" in result.stdout
        ) or (
            "✅ auto-coder-qdrant は既に" in result.stdout and "に接続されています" in result.stdout
        )
        assert network_connected, "Qdrantコンテナのネットワーク接続メッセージが見つかりません"


def test_check_graphrag_services_direct_only():
    """--direct-only オプションで直接アクセスのみテストすることを確認"""
    script_path = Path(__file__).parent.parent / "scripts" / "check_graphrag_services.py"
    python_exe = get_python_executable()

    # --direct-only オプション付きで実行
    result = subprocess.run(
        [python_exe, str(script_path), "--direct-only"],
        capture_output=True,
        text=True,
        timeout=120
    )

    # 終了コードが0であることを確認
    assert result.returncode == 0, f"スクリプトが失敗しました:\nstdout: {result.stdout}\nstderr: {result.stderr}"

    # 直接アクセステストのみが実行されることを確認
    assert "✅ Neo4j 直接アクセステスト完了" in result.stdout, "Neo4jテストの成功メッセージが見つかりません"
    assert "✅ Qdrant 直接アクセステスト完了" in result.stdout, "Qdrantテストの成功メッセージが見つかりません"
    assert "neo4j: ✅ 成功" in result.stdout, "Neo4jの最終結果が成功ではありません"
    assert "qdrant: ✅ 成功" in result.stdout, "Qdrantの最終結果が成功ではありません"

    # GraphRAG MCPテストは実行されないことを確認
    assert "GraphRAG MCP 経由アクセステスト" not in result.stdout, "GraphRAG MCPテストが実行されています"
    assert "graphrag_mcp:" not in result.stdout, "GraphRAG MCPの結果が含まれています"


def test_check_graphrag_services_mcp_only():
    """--mcp-only オプションでMCPのみテストすることを確認"""
    script_path = Path(__file__).parent.parent / "scripts" / "check_graphrag_services.py"
    python_exe = get_python_executable()

    # --mcp-only オプション付きで実行
    result = subprocess.run(
        [python_exe, str(script_path), "--mcp-only"],
        capture_output=True,
        text=True,
        timeout=120
    )

    # 終了コードが0であることを確認
    assert result.returncode == 0, f"スクリプトが失敗しました:\nstdout: {result.stdout}\nstderr: {result.stderr}"

    # GraphRAG MCPテストのみが実行されることを確認
    assert "✅ GraphRAG MCP テスト完了" in result.stdout, "GraphRAG MCPテストの成功メッセージが見つかりません"
    assert "graphrag_mcp: ✅ 成功" in result.stdout, "GraphRAG MCPの最終結果が成功ではありません"

    # 直接アクセステストは実行されないことを確認
    assert "Neo4j 直接アクセステスト" not in result.stdout, "Neo4jテストが実行されています"
    assert "Qdrant 直接アクセステスト" not in result.stdout, "Qdrantテストが実行されています"
    assert "neo4j:" not in result.stdout, "Neo4jの結果が含まれています"
    assert "qdrant:" not in result.stdout, "Qdrantの結果が含まれています"


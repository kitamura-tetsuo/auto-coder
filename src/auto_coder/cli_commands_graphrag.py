"""GraphRAG-related CLI commands."""

import subprocess
from pathlib import Path
from typing import Optional

import click

from .logger_config import get_logger, setup_logger

logger = get_logger(__name__)


def run_graphrag_setup_mcp_programmatically(
    install_dir: Optional[str] = None,
    neo4j_uri: str = "bolt://localhost:7687",
    neo4j_user: str = "neo4j",
    neo4j_password: str = "password",
    qdrant_url: str = "http://localhost:6333",
    skip_clone: bool = False,
    backends: Optional[list] = None,
    silent: bool = False,
) -> bool:
    """GraphRAG MCP サーバーをプログラム的にセットアップします。

    Args:
        install_dir: インストール先ディレクトリ（デフォルト: ~/graphrag_mcp）
        neo4j_uri: Neo4j 接続URI
        neo4j_user: Neo4j ユーザー名
        neo4j_password: Neo4j パスワード
        qdrant_url: Qdrant 接続URL
        skip_clone: 既存のディレクトリを使用（コピーをスキップ）
        backends: 設定するバックエンドのリスト（デフォルト: 全て）
        silent: True の場合、ユーザー確認をスキップして自動実行

    Returns:
        True if setup was successful, False otherwise
    """
    # Use new MCP manager for setup
    try:
        # Determine installation directory
        if install_dir is None:
            install_dir = str(Path.home() / "graphrag_mcp")

        install_path = Path(install_dir)

        if not silent:
            logger.info("GraphRAG MCP サーバーのセットアップを開始します...")
            logger.info(f"インストール先: {install_path}")

        # Check if uv is available, install if not found
        uv_available = False
        try:
            result = subprocess.run(
                ["uv", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                uv_available = True
                if not silent:
                    logger.info(f"✅ uv が利用可能です: {result.stdout.strip()}")
        except FileNotFoundError:
            pass
        except subprocess.TimeoutExpired:
            logger.error("uv コマンドがタイムアウトしました")
            return False

        # Auto-install uv if not available
        if not uv_available:
            if not silent:
                logger.warning("uv が見つかりません。自動インストールを試みます...")

            try:
                # Install uv using the official installer
                install_cmd = "curl -LsSf https://astral.sh/uv/install.sh | sh"
                result = subprocess.run(
                    install_cmd,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=300,  # 5 minutes timeout for installation
                )

                if result.returncode != 0:
                    logger.error("uv の自動インストールに失敗しました。")
                    logger.error(f"エラー: {result.stderr}")
                    logger.error(
                        "手動でインストールしてください: https://docs.astral.sh/uv/"
                    )
                    return False

                # Verify installation
                # Add common uv installation paths to PATH for this session
                import os

                uv_bin_paths = [
                    str(Path.home() / ".local" / "bin"),
                    str(Path.home() / ".cargo" / "bin"),
                ]
                current_path = os.environ.get("PATH", "")
                os.environ["PATH"] = ":".join(uv_bin_paths + [current_path])

                # Check if uv is now available
                try:
                    result = subprocess.run(
                        ["uv", "--version"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    if result.returncode == 0:
                        if not silent:
                            logger.info(
                                f"✅ uv を自動インストールしました: {result.stdout.strip()}"
                            )
                    else:
                        logger.error(
                            "uv のインストールは完了しましたが、実行できません。"
                        )
                        logger.error("シェルを再起動してから再度お試しください。")
                        return False
                except FileNotFoundError:
                    logger.error(
                        "uv のインストールは完了しましたが、PATH に見つかりません。"
                    )
                    logger.error("シェルを再起動してから再度お試しください。")
                    logger.error(
                        f"または、以下のパスを PATH に追加してください: {':'.join(uv_bin_paths)}"
                    )
                    return False

            except subprocess.TimeoutExpired:
                logger.error("uv のインストールがタイムアウトしました")
                return False
            except Exception as e:
                logger.error(f"uv のインストール中にエラーが発生しました: {e}")
                logger.error(
                    "手動でインストールしてください: https://docs.astral.sh/uv/"
                )
                return False

        # Copy bundled MCP server if needed
        if not skip_clone:
            if install_path.exists():
                if not silent:
                    logger.warning(f"ディレクトリ {install_path} は既に存在します。")
                    logger.info("既存のディレクトリを使用します（--skip-clone）")
                skip_clone = True
            else:
                if not silent:
                    logger.info("バンドルされたMCPサーバーをコピーしています...")
                try:
                    # Find the bundled MCP server in the package
                    import auto_coder

                    package_dir = Path(auto_coder.__file__).parent
                    bundled_mcp = package_dir / "mcp_servers" / "graphrag_mcp"

                    if not bundled_mcp.exists():
                        logger.error(
                            f"バンドルされたMCPサーバーが見つかりません: {bundled_mcp}"
                        )
                        logger.error(
                            "パッケージが正しくインストールされていない可能性があります。"
                        )
                        return False

                    # Copy the bundled MCP server to install directory
                    import shutil

                    shutil.copytree(
                        bundled_mcp,
                        install_path,
                        symlinks=False,
                        ignore_dangling_symlinks=True,
                    )

                    if not silent:
                        logger.info("✅ MCPサーバーをコピーしました")
                        logger.info(f"   ソース: {bundled_mcp}")
                        logger.info(f"   コピー先: {install_path}")
                except Exception as e:
                    logger.error(f"MCPサーバーのコピーに失敗しました: {e}")
                    return False
        else:
            if not install_path.exists():
                logger.error(
                    f"ディレクトリ {install_path} が存在しません。--skip-clone を使用する場合は、"
                    "事前にMCPサーバーをセットアップしてください。"
                )
                return False
            if not silent:
                logger.info(f"既存のディレクトリを使用します: {install_path}")

        # Install dependencies with uv
        if not silent:
            logger.info("依存関係をインストールしています...")
        try:
            result = subprocess.run(
                ["uv", "sync"],
                cwd=str(install_path),
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                logger.error(f"依存関係のインストールに失敗しました:\n{result.stderr}")
                return False
            if not silent:
                logger.info("✅ 依存関係をインストールしました")
        except subprocess.TimeoutExpired:
            logger.error("uv sync がタイムアウトしました")
            return False

        # Create .env file
        env_path = install_path / ".env"
        env_content = f"""# GraphRAG MCP Server Configuration
# Auto-generated by auto-coder graphrag setup-mcp

# Neo4j Configuration
NEO4J_URI={neo4j_uri}
NEO4J_USER={neo4j_user}
NEO4J_PASSWORD={neo4j_password}

# Qdrant Configuration
QDRANT_URL={qdrant_url}

# Optional: OpenAI API Key for embeddings (if using OpenAI)
# OPENAI_API_KEY=your-api-key-here
"""

        try:
            with open(env_path, "w", encoding="utf-8") as f:
                f.write(env_content)
            if not silent:
                logger.info(f"✅ .env ファイルを作成しました: {env_path}")
        except Exception as e:
            logger.error(f".env ファイルの作成に失敗しました: {e}")
            return False

        # Create run_server.sh script
        run_script_path = install_path / "run_server.sh"

        # Find uv executable path for the script
        uv_path = shutil.which("uv")
        if not uv_path:
            # Try common locations
            common_paths = [
                Path.home() / ".local" / "bin" / "uv",
                Path.home() / ".cargo" / "bin" / "uv",
                Path("/usr/local/bin/uv"),
            ]
            for path in common_paths:
                if path.exists():
                    uv_path = str(path)
                    break

        if not uv_path:
            logger.error("uv executable not found in PATH or common locations")
            return False

        # Create a robust run_server.sh that can find uv even in pipx environments
        run_script_content = f"""#!/bin/bash
# GraphRAG MCP Server startup script
# This script ensures the .env file is loaded from the correct directory

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"

# Change to the script directory to ensure .env is loaded correctly
cd "$SCRIPT_DIR"

# Clear VIRTUAL_ENV to avoid conflicts with other projects
unset VIRTUAL_ENV

# Add common uv installation paths to PATH
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:$PATH"

# Try to find uv executable
UV_CMD=""

# First, try the path we found during setup
if [ -x "{uv_path}" ]; then
    UV_CMD="{uv_path}"
# Then try common locations
elif command -v uv >/dev/null 2>&1; then
    UV_CMD="uv"
elif [ -x "$HOME/.local/bin/uv" ]; then
    UV_CMD="$HOME/.local/bin/uv"
elif [ -x "$HOME/.cargo/bin/uv" ]; then
    UV_CMD="$HOME/.cargo/bin/uv"
elif [ -x "/usr/local/bin/uv" ]; then
    UV_CMD="/usr/local/bin/uv"
else
    echo "Error: uv executable not found" >&2
    echo "Please install uv: https://docs.astral.sh/uv/" >&2
    exit 1
fi

# Run the MCP server with uv
exec "$UV_CMD" run main.py
"""

        try:
            with open(run_script_path, "w", encoding="utf-8") as f:
                f.write(run_script_content)
            # Make the script executable
            import os

            os.chmod(run_script_path, 0o755)
            if not silent:
                logger.info(
                    f"✅ run_server.sh スクリプトを作成しました: {run_script_path}"
                )
        except Exception as e:
            logger.error(f"run_server.sh スクリプトの作成に失敗しました: {e}")
            return False

        # Patch main.py to load .env from script directory
        main_py_path = install_path / "main.py"
        main_py_content = """import os
from pathlib import Path
from dotenv import load_dotenv

def main():
    # Get the directory where this script is located
    script_dir = Path(__file__).parent.resolve()
    env_path = script_dir / ".env"

    # Load environment variables from .env file in script directory
    load_dotenv(env_path)

    # Import here to avoid circular imports
    from server import mcp

    # Run the MCP server directly
    mcp.run()

if __name__ == "__main__":
    main()
"""

        try:
            with open(main_py_path, "w", encoding="utf-8") as f:
                f.write(main_py_content)
            if not silent:
                logger.info(f"✅ main.py を修正しました（.envパスを明示的に指定）")
        except Exception as e:
            logger.error(f"main.py の修正に失敗しました: {e}")
            return False

        if not silent:
            logger.info("=" * 60)
            logger.info("✅ GraphRAG MCP サーバーのセットアップが完了しました！")
            logger.info("=" * 60)

        # Automatically configure backends
        # If no backends specified, configure all
        if not backends:
            backends_to_configure = ["codex", "gemini", "qwen", "windsurf"]
        else:
            backends_to_configure = list(backends)

        if not silent:
            logger.info("各バックエンドの設定ファイルを自動更新しています...")

        success_count = 0
        total_count = len(backends_to_configure)

        # Configure each backend
        for backend in backends_to_configure:
            if backend == "codex":
                if _add_codex_config(install_path):
                    success_count += 1
            elif backend == "gemini":
                if _add_gemini_config(install_path):
                    success_count += 1
            elif backend == "qwen":
                if _add_qwen_config(install_path):
                    success_count += 1
            elif backend == "windsurf":
                if _add_windsurf_claude_config(install_path):
                    success_count += 1

        if not silent:
            logger.info(f"設定完了: {success_count}/{total_count} バックエンド")
            logger.info("")
            logger.info("次のステップ:")
            logger.info("1. Neo4j と Qdrant を起動:")
            logger.info("   auto-coder graphrag start")
            logger.info("")
            logger.info("2. GraphRAG を使用してコードを処理:")
            logger.info("   auto-coder process-issues --repo owner/repo")

        return success_count > 0

    except Exception as e:
        logger.error(f"セットアップ中にエラーが発生しました: {e}")
        return False


def _add_codex_config(install_path: Path) -> bool:
    """Add GraphRAG MCP configuration to Codex CLI config using CodexClient.

    Args:
        install_path: Path to graphrag_mcp installation

    Returns:
        True if configuration was added successfully, False otherwise
    """
    try:
        from .codex_client import CodexClient

        client = CodexClient()
        result = client.add_mcp_server_config(
            "graphrag", "uv", ["run", str(install_path / "main.py")]
        )

        if result:
            logger.info("✅ Codex設定を更新しました")
        else:
            logger.error("Codex設定の更新に失敗しました")

        return result
    except Exception as e:
        logger.error(f"Failed to add Codex config: {e}")
        return False


def _add_gemini_config(install_path: Path) -> bool:
    """Add GraphRAG MCP configuration to Gemini CLI config using GeminiClient.

    Args:
        install_path: Path to graphrag_mcp installation

    Returns:
        True if configuration was added successfully, False otherwise
    """
    try:
        from .gemini_client import GeminiClient

        # GeminiClient requires API key, but we only need add_mcp_server_config
        # which uses CLI commands, so we can pass None
        client = GeminiClient(api_key=None)

        # Use uv with --directory option to ensure correct working directory
        result = client.add_mcp_server_config(
            "graphrag", "uv", ["--directory", str(install_path), "run", "main.py"]
        )

        if result:
            logger.info("✅ Gemini設定を更新しました")
        else:
            logger.error("Gemini設定の更新に失敗しました")

        return result
    except Exception as e:
        logger.error(f"Failed to add Gemini config: {e}")
        return False


def _add_qwen_config(install_path: Path) -> bool:
    """Add GraphRAG MCP configuration to Qwen CLI config using QwenClient.

    Args:
        install_path: Path to graphrag_mcp installation

    Returns:
        True if configuration was added successfully, False otherwise
    """
    try:
        from .qwen_client import QwenClient

        client = QwenClient()

        # Use run_server.sh if it exists (for compatibility and to avoid VIRTUAL_ENV issues)
        # Qwen supports shell scripts directly
        run_script = install_path / "run_server.sh"
        if run_script.exists():
            result = client.add_mcp_server_config("graphrag", str(run_script), [])
        else:
            # Fallback to uv with --directory option
            result = client.add_mcp_server_config(
                "graphrag", "uv", ["--directory", str(install_path), "run", "main.py"]
            )

        if result:
            logger.info("✅ Qwen設定を更新しました")
        else:
            logger.error("Qwen設定の更新に失敗しました")

        return result
    except Exception as e:
        logger.error(f"Failed to add Qwen config: {e}")
        return False


def _add_windsurf_claude_config(install_path: Path) -> bool:
    """Add GraphRAG MCP configuration to Windsurf/Claude config using AuggieClient.

    Args:
        install_path: Path to graphrag_mcp installation

    Returns:
        True if configuration was added successfully, False otherwise
    """
    try:
        from .auggie_client import AuggieClient

        client = AuggieClient()

        # Use run_server.sh if it exists (for compatibility)
        # Windsurf/Claude supports shell scripts directly
        run_script = install_path / "run_server.sh"
        if run_script.exists():
            result = client.add_mcp_server_config("graphrag", str(run_script), [])
        else:
            # Fallback to uv with --directory option
            result = client.add_mcp_server_config(
                "graphrag", "uv", ["--directory", str(install_path), "run", "main.py"]
            )

        if result:
            logger.info("✅ Windsurf/Claude設定を更新しました")
        else:
            logger.error("Windsurf/Claude設定の更新に失敗しました")

        return result
    except Exception as e:
        logger.error(f"Failed to add Windsurf/Claude config: {e}")
        return False


@click.group(name="graphrag")
def graphrag_group() -> None:
    """GraphRAG (Neo4j + Qdrant) 管理コマンド。

    - start: Docker コンテナを起動
    - stop: Docker コンテナを停止
    - status: コンテナの状態を確認
    - update-index: コードベースのインデックスを更新
    - setup-mcp: GraphRAG MCP サーバーを自動セットアップ
    """
    pass


@graphrag_group.command("start")
@click.option(
    "--wait/--no-wait",
    default=True,
    help="コンテナがヘルシーになるまで待機するか",
)
@click.option(
    "--timeout",
    type=int,
    default=120,
    help="ヘルスチェックのタイムアウト（秒）",
)
def graphrag_start(wait: bool, timeout: int) -> None:
    """Neo4j と Qdrant の Docker コンテナを起動します。"""
    from .graphrag_docker_manager import GraphRAGDockerManager

    setup_logger()

    click.echo("Starting GraphRAG Docker containers (Neo4j and Qdrant)...")

    try:
        manager = GraphRAGDockerManager()
    except RuntimeError as e:
        click.echo()
        click.echo(f"❌ {e}")
        raise click.ClickException("Docker Compose is not available")

    try:
        success = manager.start(wait_for_health=wait, timeout=timeout)
        if success:
            click.echo("✅ GraphRAG containers started successfully")
            if wait:
                status = manager.get_status()
                click.echo(
                    f"   Neo4j: {'✅ healthy' if status['neo4j'] else '❌ unhealthy'}"
                )
                click.echo(
                    f"   Qdrant: {'✅ healthy' if status['qdrant'] else '❌ unhealthy'}"
                )

                # Check if any container is unhealthy
                if not status["neo4j"] or not status["qdrant"]:
                    click.echo()
                    click.echo(
                        "⚠️  Some containers are unhealthy. Troubleshooting tips:"
                    )
                    click.echo(
                        "   1. Check Docker logs: docker compose -f docker-compose.graphrag.yml logs"
                    )
                    click.echo(
                        "   2. Verify ports are not in use: lsof -i :7474 -i :7687 -i :6333"
                    )
                    click.echo(
                        "   3. Try restarting: auto-coder graphrag stop && auto-coder graphrag start"
                    )
        else:
            click.echo()
            click.echo("❌ Failed to start GraphRAG containers")
            click.echo()
            click.echo("Troubleshooting tips:")
            click.echo("   1. Ensure Docker is running: docker ps")
            click.echo("   2. Check Docker permissions: sudo usermod -aG docker $USER")
            click.echo("      (then logout and login again)")
            click.echo(
                "   3. Check docker-compose.graphrag.yml exists in repository root"
            )
            click.echo(
                "   4. Check Docker logs: docker compose -f docker-compose.graphrag.yml logs"
            )
            click.echo(
                "   5. Try manual start: docker compose -f docker-compose.graphrag.yml up -d"
            )
            raise click.ClickException("Failed to start GraphRAG containers")
    except click.ClickException:
        raise
    except Exception as e:
        click.echo()
        click.echo(f"❌ Error starting containers: {e}")
        click.echo()
        click.echo("Troubleshooting tips:")
        click.echo("   1. Ensure Docker is installed and running")
        click.echo("   2. Check if docker compose is available: docker compose version")
        click.echo("   3. Check Docker permissions: sudo usermod -aG docker $USER")
        click.echo("      (then logout and login again)")
        click.echo("   4. Verify docker-compose.graphrag.yml exists")
        raise click.ClickException(f"Error starting containers: {e}")


@graphrag_group.command("stop")
@click.option(
    "--timeout",
    type=int,
    default=60,
    help="コマンドのタイムアウト（秒）",
)
def graphrag_stop(timeout: int) -> None:
    """Neo4j と Qdrant の Docker コンテナを停止します。"""
    from .graphrag_docker_manager import GraphRAGDockerManager

    setup_logger()
    manager = GraphRAGDockerManager()

    click.echo("Stopping GraphRAG Docker containers...")

    try:
        success = manager.stop(timeout=timeout)
        if success:
            click.echo("✅ GraphRAG containers stopped successfully")
        else:
            raise click.ClickException("Failed to stop GraphRAG containers")
    except Exception as e:
        raise click.ClickException(f"Error stopping containers: {e}")


@graphrag_group.command("status")
def graphrag_status() -> None:
    """Neo4j と Qdrant の Docker コンテナの状態を確認します。"""
    from .graphrag_docker_manager import GraphRAGDockerManager

    setup_logger()
    manager = GraphRAGDockerManager()

    click.echo("Checking GraphRAG Docker containers status...")
    click.echo()

    try:
        is_running = manager.is_running()
        if is_running:
            click.echo("📦 Containers: ✅ Running")
            status = manager.get_status()
            click.echo(
                f"   Neo4j: {'✅ healthy' if status['neo4j'] else '❌ unhealthy'}"
            )
            click.echo(
                f"   Qdrant: {'✅ healthy' if status['qdrant'] else '❌ unhealthy'}"
            )
        else:
            click.echo("📦 Containers: ❌ Not running")
            click.echo("   Run 'auto-coder graphrag start' to start containers")
    except Exception as e:
        raise click.ClickException(f"Error checking status: {e}")


@graphrag_group.command("update-index")
@click.option(
    "--force",
    is_flag=True,
    help="インデックスが最新でも強制的に更新",
)
@click.option(
    "--repo-path",
    type=click.Path(exists=True, file_okay=False, dir_okay=True),
    help="インデックス対象のリポジトリパス（デフォルト: カレントディレクトリ）",
)
def graphrag_update_index(force: bool, repo_path: Optional[str]) -> None:
    """コードベースのインデックスを Neo4j と Qdrant に更新します。"""
    from .graphrag_docker_manager import GraphRAGDockerManager
    from .graphrag_index_manager import GraphRAGIndexManager

    setup_logger()

    # Ensure Docker containers are running
    docker_manager = GraphRAGDockerManager()
    if not docker_manager.is_running():
        click.echo("⚠️  GraphRAG containers are not running")
        if click.confirm("Start containers now?"):
            click.echo("Starting containers...")
            if not docker_manager.start(wait_for_health=True):
                raise click.ClickException("Failed to start containers")
            click.echo("✅ Containers started")
        else:
            raise click.ClickException(
                "Containers must be running to update index. Run 'auto-coder graphrag start' first."
            )

    # Update index
    try:
        index_manager = GraphRAGIndexManager(repo_path=repo_path)
    except Exception as e:
        click.echo()
        click.echo(f"❌ Error initializing index manager: {e}")
        click.echo()
        click.echo("Troubleshooting tips:")
        click.echo("   1. Verify the repository path exists and is accessible")
        click.echo("   2. Check file permissions in the repository")
        raise click.ClickException(f"Error initializing index manager: {e}")

    # Check if indexed path matches current path
    try:
        path_matches, indexed_path = index_manager.check_indexed_path()
        if indexed_path is not None and not path_matches:
            click.echo()
            click.echo("⚠️  インデックス対象ディレクトリが異なります:")
            click.echo(f"   インデックス済み: {indexed_path}")
            click.echo(f"   現在のディレクトリ: {index_manager.repo_path.resolve()}")
            click.echo()
            if not force and not click.confirm(
                "現在のディレクトリでインデックスを更新しますか?"
            ):
                click.echo("インデックス更新をキャンセルしました")
                return
            force = True  # Force update when path changes
    except Exception as e:
        click.echo(f"⚠️  Warning: Could not check indexed path: {e}")

    click.echo("Updating GraphRAG index...")
    if not force:
        try:
            if index_manager.is_index_up_to_date():
                click.echo("✅ Index is already up to date")
                click.echo("   Use --force to update anyway")
                return
        except Exception as e:
            click.echo(f"⚠️  Warning: Could not check index status: {e}")
            click.echo("   Proceeding with update...")

    try:
        success = index_manager.update_index(force=force)
        if success:
            click.echo("✅ Index updated successfully")
            click.echo()
            click.echo("Note: Current implementation uses hash-based change detection.")
            click.echo(
                "      Full semantic indexing (embeddings, Neo4j/Qdrant storage)"
            )
            click.echo("      is planned for future enhancement.")
        else:
            click.echo()
            click.echo("❌ Failed to update index")
            click.echo()
            click.echo("Troubleshooting tips:")
            click.echo(
                "   1. Check if containers are healthy: auto-coder graphrag status"
            )
            click.echo("   2. Verify repository contains Python files")
            click.echo("   3. Check logs for detailed error messages")
            raise click.ClickException("Failed to update index")
    except click.ClickException:
        raise
    except Exception as e:
        click.echo()
        click.echo(f"❌ Error updating index: {e}")
        click.echo()
        click.echo("Troubleshooting tips:")
        click.echo("   1. Check if containers are running and healthy")
        click.echo("   2. Verify file permissions in repository")
        click.echo("   3. Check available disk space")
        raise click.ClickException(f"Error updating index: {e}")


@graphrag_group.command("setup-mcp")
@click.option(
    "--install-dir",
    type=click.Path(file_okay=False, dir_okay=True, resolve_path=True),
    default=None,
    help="GraphRAG MCP サーバーのインストール先ディレクトリ（デフォルト: ~/graphrag_mcp）",
)
@click.option(
    "--neo4j-uri",
    default="bolt://localhost:7687",
    help="Neo4j 接続URI（デフォルト: bolt://localhost:7687）",
)
@click.option(
    "--neo4j-user",
    default="neo4j",
    help="Neo4j ユーザー名（デフォルト: neo4j）",
)
@click.option(
    "--neo4j-password",
    default="password",
    help="Neo4j パスワード（デフォルト: password）",
)
@click.option(
    "--qdrant-url",
    default="http://localhost:6333",
    help="Qdrant 接続URL（デフォルト: http://localhost:6333）",
)
@click.option(
    "--skip-clone",
    is_flag=True,
    help="既存のディレクトリを使用（クローンをスキップ）",
)
@click.option(
    "--backends",
    multiple=True,
    type=click.Choice(["codex", "gemini", "qwen", "windsurf"], case_sensitive=False),
    help="設定するバックエンドを指定（デフォルト: 全て）",
)
def graphrag_setup_mcp(
    install_dir: Optional[str],
    neo4j_uri: str,
    neo4j_user: str,
    neo4j_password: str,
    qdrant_url: str,
    skip_clone: bool,
    backends: tuple,
) -> None:
    """GraphRAG MCP サーバーを自動セットアップします。

    このコマンドは以下を実行します：
    1. バンドルされたカスタムMCPサーバー（コード分析専用フォーク）をコピー
    2. uv を使用して依存関係をインストール
    3. .env ファイルを作成して接続情報を設定
    4. 各バックエンド（Codex, Gemini, Qwen, Windsurf/Claude）の設定ファイルを自動更新

    注: このMCPサーバーは rileylemm/graphrag_mcp のカスタムフォークで、
    TypeScript/JavaScriptコード分析に特化しています。
    """
    setup_logger()

    # Handle interactive confirmation for existing directory
    if install_dir is None:
        install_dir = str(Path.home() / "graphrag_mcp")

    install_path = Path(install_dir)

    # Interactive confirmation if directory exists and not skip_clone
    if not skip_clone and install_path.exists():
        if not click.confirm(
            f"ディレクトリ {install_path} は既に存在します。削除して再クローンしますか？"
        ):
            click.echo("セットアップをキャンセルしました")
            return

        # Remove existing directory
        import shutil

        try:
            shutil.rmtree(install_path)
            click.echo(f"既存のディレクトリを削除しました: {install_path}")
        except Exception as e:
            raise click.ClickException(f"ディレクトリの削除に失敗しました: {e}")

    # Call the programmatic setup function
    success = run_graphrag_setup_mcp_programmatically(
        install_dir=install_dir,
        neo4j_uri=neo4j_uri,
        neo4j_user=neo4j_user,
        neo4j_password=neo4j_password,
        qdrant_url=qdrant_url,
        skip_clone=skip_clone,
        backends=list(backends) if backends else None,
        silent=False,
    )

    if not success:
        raise click.ClickException("セットアップに失敗しました")

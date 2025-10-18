"""GraphRAG-related CLI commands."""

from typing import Optional

import click

from .logger_config import setup_logger


@click.group(name="graphrag")
def graphrag_group() -> None:
    """GraphRAG (Neo4j + Qdrant) 管理コマンド。

    - start: Docker コンテナを起動
    - stop: Docker コンテナを停止
    - status: コンテナの状態を確認
    - update-index: コードベースのインデックスを更新
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
                click.echo(f"   Neo4j: {'✅ healthy' if status['neo4j'] else '❌ unhealthy'}")
                click.echo(
                    f"   Qdrant: {'✅ healthy' if status['qdrant'] else '❌ unhealthy'}"
                )

                # Check if any container is unhealthy
                if not status['neo4j'] or not status['qdrant']:
                    click.echo()
                    click.echo("⚠️  Some containers are unhealthy. Troubleshooting tips:")
                    click.echo("   1. Check Docker logs: docker compose -f docker-compose.graphrag.yml logs")
                    click.echo("   2. Verify ports are not in use: lsof -i :7474 -i :7687 -i :6333")
                    click.echo("   3. Try restarting: auto-coder graphrag stop && auto-coder graphrag start")
        else:
            click.echo()
            click.echo("❌ Failed to start GraphRAG containers")
            click.echo()
            click.echo("Troubleshooting tips:")
            click.echo("   1. Ensure Docker is running: docker ps")
            click.echo("   2. Check Docker permissions: sudo usermod -aG docker $USER")
            click.echo("      (then logout and login again)")
            click.echo("   3. Check docker-compose.graphrag.yml exists in repository root")
            click.echo("   4. Check Docker logs: docker compose -f docker-compose.graphrag.yml logs")
            click.echo("   5. Try manual start: docker compose -f docker-compose.graphrag.yml up -d")
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
            click.echo(f"   Neo4j: {'✅ healthy' if status['neo4j'] else '❌ unhealthy'}")
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
            click.echo("      Full semantic indexing (embeddings, Neo4j/Qdrant storage)")
            click.echo("      is planned for future enhancement.")
        else:
            click.echo()
            click.echo("❌ Failed to update index")
            click.echo()
            click.echo("Troubleshooting tips:")
            click.echo("   1. Check if containers are healthy: auto-coder graphrag status")
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


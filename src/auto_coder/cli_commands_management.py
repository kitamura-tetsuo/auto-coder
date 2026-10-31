"""CLI for the independent management resource server."""

import sys
from pathlib import Path

import click


@click.group("management")
def management_group() -> None:
    """Run authenticated remote management services."""


@management_group.command("serve")
@click.option("--config", "config_path", required=True, type=click.Path(path_type=Path, exists=True, dir_okay=False))
def management_serve(config_path: Path) -> None:
    """Serve the management API from a validated configuration."""

    import uvicorn

    from .logger_config import setup_logger
    from .management.app import create_management_app
    from .management.oauth_config import load_management_settings

    try:
        settings = load_management_settings(config_path)
        settings.state_root.mkdir(parents=True, exist_ok=True)
        app = create_management_app(settings)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise click.ClickException(f"invalid management configuration: {exc}") from exc
    setup_logger(stream=sys.stdout, log_file=str(settings.state_root / "logs" / "management.log"))
    uvicorn.run(app, host=settings.bind_host, port=settings.port, access_log=False)


@management_group.command("recover")
@click.option("--config", "config_path", required=True, type=click.Path(path_type=Path, exists=True, dir_okay=False))
@click.option("--request-id", required=True)
@click.option("--expected-supervisor", required=True)
@click.option("--expected-generation", type=int, required=True)
@click.option("--expected-runtime", required=True)
@click.option("--expected-receipt", required=True)
@click.option("--expected-source-receipt", default="", show_default=True)
def management_recover(config_path: Path, request_id: str, expected_supervisor: str, expected_generation: int, expected_runtime: str, expected_receipt: str, expected_source_receipt: str) -> None:
    """Request one local recovery of the already selected management child."""
    import hashlib
    import json
    import sqlite3
    from dataclasses import asdict

    from .management.bootstrap_supervisor import BootstrapConfig
    from .management.oauth_config import load_management_settings
    from .management.recovery_mailbox import RecoveryIntent, RecoveryMailbox

    try:
        settings = load_management_settings(config_path)
        if settings.bootstrap_config_path is None or settings.config_path is None:
            raise ValueError("management bootstrap is not configured")
        bootstrap = BootstrapConfig.read(settings.bootstrap_config_path)
        if bootstrap.oauth_root != settings.state_root or bootstrap.journal_root != settings.operation_state_root or (bootstrap.management_config is not None and bootstrap.management_config != settings.config_path):
            raise ValueError("management and bootstrap configuration differ")
        intent = RecoveryIntent(request_id, bootstrap.instance_id, expected_supervisor, expected_generation, expected_runtime, expected_receipt, expected_source_receipt, str(settings.config_path), hashlib.sha256(settings.config_path.read_bytes()).hexdigest())
        record = RecoveryMailbox(bootstrap.state_root, bootstrap.instance_id).submit(intent)
        click.echo(json.dumps(asdict(record), sort_keys=True))
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        raise click.ClickException(f"recovery request unavailable: {exc}") from exc


@management_group.command("recovery-status")
@click.option("--config", "config_path", required=True, type=click.Path(path_type=Path, exists=True, dir_okay=False))
@click.option("--request-id", required=True)
def management_recovery_status(config_path: Path, request_id: str) -> None:
    """Read one locally submitted recovery request."""
    import json
    import sqlite3
    from dataclasses import asdict

    from .management.bootstrap_supervisor import BootstrapConfig
    from .management.oauth_config import load_management_settings
    from .management.recovery_mailbox import RecoveryMailbox

    try:
        settings = load_management_settings(config_path)
        if settings.bootstrap_config_path is None:
            raise ValueError("management bootstrap is not configured")
        bootstrap = BootstrapConfig.read(settings.bootstrap_config_path)
        if bootstrap.oauth_root != settings.state_root or bootstrap.journal_root != settings.operation_state_root or (bootstrap.management_config is not None and bootstrap.management_config != settings.config_path):
            raise ValueError("management and bootstrap configuration differ")
        record = RecoveryMailbox(bootstrap.state_root, bootstrap.instance_id).get(request_id)
        if record is None:
            raise ValueError("recovery request not found")
        click.echo(json.dumps(asdict(record), sort_keys=True))
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        raise click.ClickException(f"recovery status unavailable: {exc}") from exc


@management_group.command("fresh-status")
@click.option("--config", "config_path", required=True, type=click.Path(path_type=Path, exists=True, dir_okay=False))
def management_fresh_status(config_path: Path) -> None:
    """Read local fresh-initialization launch and authenticated readiness."""
    import json
    import sqlite3

    from .management.bootstrap_supervisor import BootstrapConfig
    from .management.fresh_initialization import read_fresh_status
    from .management.oauth_config import load_management_settings

    try:
        settings = load_management_settings(config_path)
        if settings.bootstrap_config_path is None or settings.config_path is None:
            raise ValueError("management bootstrap is not configured")
        bootstrap = BootstrapConfig.read(settings.bootstrap_config_path)
        if bootstrap.oauth_root != settings.state_root or bootstrap.journal_root != settings.operation_state_root or (bootstrap.management_config is not None and bootstrap.management_config != settings.config_path):
            raise ValueError("management and bootstrap configuration differ")
        click.echo(json.dumps(read_fresh_status(bootstrap.state_root), sort_keys=True))
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        raise click.ClickException(f"fresh status unavailable: {exc}") from exc

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

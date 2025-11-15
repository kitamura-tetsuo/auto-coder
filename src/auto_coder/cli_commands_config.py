"""Configuration CLI commands for LLM backend management."""

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import click

from .llm_backend_config import LLMBackendConfig, LLMBackendConfigManager, ensure_config_directory
from .logger_config import get_config_logger

logger = get_config_logger()


def get_editor() -> str:
    """Get the preferred editor from environment or system defaults."""
    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "nano"))
    return editor


def create_interactive_config_wizard() -> LLMBackendConfig:
    """Create a new configuration through an interactive wizard."""
    config = LLMBackendConfig()

    click.echo("Welcome to the Auto-Coder Configuration Wizard!")
    click.echo("=" * 50)
    click.echo()

    # Ask for default backend
    backends = ["codex", "codex-mcp", "gemini", "qwen", "claude", "auggie"]
    click.echo("Available backends: " + ", ".join(backends))

    while True:
        default_backend = click.prompt("Enter default backend", default="codex", type=click.Choice(backends))
        # Normalize the backend name for internal use
        normalized_backend = default_backend.replace("-", "_")
        break

    # Ask for configuration for each backend
    backend_config_map = {"codex": config.codex, "codex_mcp": config.codex_mcp, "gemini": config.gemini, "qwen": config.qwen, "claude": config.claude, "auggie": config.auggie}

    for backend_name, backend_config in backend_config_map.items():
        click.echo(f"\nConfiguring {backend_name}:")

        if click.confirm(f"Do you want to configure the API key for {backend_name}?", default=False):
            api_key = click.prompt("Enter API key", hide_input=True)
            backend_config.api_key = api_key

        if click.confirm(f"Do you want to configure the model for {backend_name}?", default=False):
            model = click.prompt("Enter model name", default="")
            if model:
                backend_config.model = model

        if click.confirm(f"Do you want to configure the base URL for {backend_name}?", default=False):
            base_url = click.prompt("Enter base URL", default="")
            if base_url:
                backend_config.base_url = base_url

        if click.confirm(f"Do you want to configure the temperature for {backend_name}?", default=False):
            temperature = click.prompt("Enter temperature (0.0-1.0)", default=str(0.7), type=float)
            backend_config.temperature = temperature

    click.echo("\nConfiguration wizard completed!")
    return config


def print_config_table(config: LLMBackendConfig) -> None:
    """Print the configuration in a formatted table."""
    from rich.console import Console
    from rich.table import Table

    console = Console()

    table = Table(title="LLM Backend Configuration")
    table.add_column("Backend", style="cyan")
    table.add_column("API Key", style="magenta")
    table.add_column("Model", style="green")
    table.add_column("Base URL", style="blue")
    table.add_column("Temperature", style="yellow")

    backends = [
        ("codex", config.codex),
        ("codex_mcp", config.codex_mcp),
        ("gemini", config.gemini),
        ("qwen", config.qwen),
        ("claude", config.claude),
        ("auggie", config.auggie),
    ]

    for backend_name, backend_config in backends:
        api_key_display = "SET" if backend_config.api_key else "NOT SET"
        model_display = backend_config.model or "DEFAULT"
        base_url_display = backend_config.base_url or "DEFAULT"
        temperature_display = str(backend_config.temperature) if backend_config.temperature is not None else "DEFAULT"

        table.add_row(backend_name.replace("_", "-"), api_key_display, model_display, base_url_display, temperature_display)

    console.print(table)


@click.group()
def config_group() -> None:
    """Configuration management for LLM backends.

    Examples:

    # Show current configuration
    auto-coder config show

    # Edit configuration file interactively
    auto-coder config edit

    # Validate configuration file
    auto-coder config validate

    # Reset to default configuration
    auto-coder config reset

    # Show differences between config files
    auto-coder config diff --other /path/to/other/config.toml

    # Import configuration from another file
    auto-coder config import /path/to/config.toml --backup

    # Export current configuration to another file
    auto-coder config export /path/to/backup/config.toml

    # Check configuration health
    auto-coder config health
    """
    pass


@config_group.command()
@click.option("--file", "-f", type=click.Path(exists=True), help="Configuration file to show")
def show(file: Optional[str]) -> None:
    """Show current configuration.

    Examples:

    \b
    # Show current configuration
    auto-coder config show

    \b
    # Show specific configuration file
    auto-coder config show --file /path/to/config.toml
    """
    config_path = Path(file) if file else None
    config = LLMBackendConfig.load_from_file(config_path)

    click.echo("Current LLM Backend Configuration:")
    click.echo("=" * 40)

    print_config_table(config)

    click.echo("\nConfiguration file path:")
    if config_path:
        click.echo(f"  {config_path}")
    else:
        click.echo(f"  {LLMBackendConfig.DEFAULT_CONFIG_FILE}")

    # Show validation status
    validation_errors = config.validate_config()
    if validation_errors:
        click.echo("\nValidation errors found:")
        for error in validation_errors:
            click.echo(f"  - {error}")
    else:
        click.echo("\n✓ Configuration is valid")


@config_group.command()
@click.option("--file", "-f", type=click.Path(), help="Configuration file to edit")
@click.option("--non-interactive", is_flag=True, help="Skip interactive mode even if no config exists")
def edit(file: Optional[str], non_interactive: bool) -> None:
    """Open configuration file in editor.

    Examples:

    \b
    # Edit default configuration file
    auto-coder config edit

    \b
    # Edit specific configuration file
    auto-coder config edit --file /path/to/config.toml

    \b
    # Edit without running interactive wizard
    auto-coder config edit --non-interactive
    """
    config_path = Path(file) if file else LLMBackendConfig.DEFAULT_CONFIG_FILE

    # Create directory if it doesn't exist
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # If file doesn't exist and user hasn't specified non-interactive mode,
    # ask if they want to run the wizard or create a default config
    if not config_path.exists() and not non_interactive:
        if click.confirm("Configuration file doesn't exist. Run the interactive wizard?", default=True):
            config = create_interactive_config_wizard()
            config.save_to_file(config_path)
            click.echo(f"Configuration saved to {config_path}")
            return
        elif click.confirm("Create default configuration file?", default=True):
            config = LLMBackendConfig()
            config.save_default_config(config_path)
            click.echo(f"Default configuration created at {config_path}")

    # Determine editor
    editor = get_editor()

    # Open in editor
    try:
        result = subprocess.run([editor, str(config_path)])
        if result.returncode != 0:
            click.echo(f"Editor exited with code {result.returncode}")
    except FileNotFoundError:
        click.echo(f"Editor '{editor}' not found. Please set your EDITOR environment variable.")
        sys.exit(1)


@config_group.command()
@click.option("--file", "-f", type=click.Path(), help="Configuration file to reset")
def reset(file: Optional[str]) -> None:
    """Reset configuration to default values.

    Examples:

    \b
    # Reset default configuration file
    auto-coder config reset

    \b
    # Reset specific configuration file
    auto-coder config reset --file /path/to/config.toml
    """
    config_path = Path(file) if file else LLMBackendConfig.DEFAULT_CONFIG_FILE

    if config_path.exists():
        if not click.confirm(f"Are you sure you want to reset configuration at {config_path}?", default=False):
            click.echo("Reset cancelled.")
            return

    config = LLMBackendConfig()
    config.save_default_config(config_path)

    click.echo(f"Configuration reset to default values at {config_path}")


@config_group.command()
@click.option("--file", "-f", type=click.Path(exists=True), help="Configuration file to validate")
def validate(file: Optional[str]) -> None:
    """Validate configuration file syntax and content.

    Examples:

    \b
    # Validate default configuration file
    auto-coder config validate

    \b
    # Validate specific configuration file
    auto-coder config validate --file /path/to/config.toml
    """
    config_path = Path(file) if file else LLMBackendConfig.DEFAULT_CONFIG_FILE

    if not config_path.exists():
        click.echo(f"Configuration file does not exist: {config_path}")
        click.echo("Run 'auto-coder config edit' to create a configuration file")
        return

    try:
        config = LLMBackendConfig.load_from_file(config_path)
        validation_errors = config.validate_config()

        if validation_errors:
            click.echo("Configuration validation failed:")
            for error in validation_errors:
                click.echo(f"  - {error}")
            sys.exit(1)
        else:
            click.echo("✓ Configuration is valid")
    except Exception as e:
        click.echo(f"Configuration validation failed: {e}")
        sys.exit(1)


@config_group.command()
@click.option("--from", "from_path", type=click.Path(exists=True), required=True, help="Source configuration file")
@click.option("--to", "to_path", type=click.Path(), help="Destination configuration file")
def migrate(from_path: str, to_path: Optional[str]) -> None:
    """Migrate configuration from an existing file to new format.

    Examples:

    \b
    # Migrate configuration from one file to default location
    auto-coder config migrate --from /path/to/old/config.toml

    \b
    # Migrate configuration from one file to another
    auto-coder config migrate --from /path/to/old/config.toml --to /path/to/new/config.toml
    """
    source_path = Path(from_path)
    dest_path = Path(to_path) if to_path else LLMBackendConfig.DEFAULT_CONFIG_FILE

    click.echo(f"Migrating configuration from {source_path} to {dest_path}")

    # Load the source configuration
    try:
        # Try to load as TOML first
        import toml

        with open(source_path, "r", encoding="utf-8") as f:
            data = toml.load(f)

        # Create a new config from the data
        config = LLMBackendConfig.from_dict(data)
        click.echo("✓ Successfully loaded source configuration")
    except Exception as e:
        click.echo(f"Failed to load source configuration: {e}")
        sys.exit(1)

    # Create backup of destination if it exists
    if dest_path.exists():
        backup_path = dest_path.with_suffix(dest_path.suffix + ".backup")
        import shutil

        shutil.copy2(dest_path, backup_path)
        click.echo(f"Backup created at {backup_path}")

    # Save to destination
    try:
        config.save_to_file(dest_path)
        click.echo(f"✓ Configuration migrated successfully to {dest_path}")

        # Show validation results
        validation_errors = config.validate_config()
        if validation_errors:
            click.echo("\nValidation warnings after migration:")
            for error in validation_errors:
                click.echo(f"  - {error}")
        else:
            click.echo("\n✓ Migrated configuration is valid")
    except Exception as e:
        click.echo(f"Failed to save migrated configuration: {e}")
        sys.exit(1)


@config_group.command()
@click.option("--file", "-f", type=click.Path(exists=True), help="Configuration file to show diff for")
@click.option("--other", "-o", type=click.Path(exists=True), required=True, help="Other configuration file for comparison")
def diff(file: Optional[str], other: str) -> None:
    """Show differences between current and another configuration.

    Examples:

    \b
    # Show differences between default config and another file
    auto-coder config diff --other /path/to/other/config.toml

    \b
    # Show differences between two specific config files
    auto-coder config diff --file /path/to/config1.toml --other /path/to/config2.toml
    """
    config_path = Path(file) if file else LLMBackendConfig.DEFAULT_CONFIG_FILE
    other_path = Path(other)

    if not config_path.exists():
        click.echo(f"Configuration file does not exist: {config_path}")
        sys.exit(1)

    if not other_path.exists():
        click.echo(f"Other configuration file does not exist: {other_path}")
        sys.exit(1)

    config1 = LLMBackendConfig.load_from_file(config_path)
    config2 = LLMBackendConfig.load_from_file(other_path)

    diff_result = config1.get_diff(config2)

    if not diff_result:
        click.echo("No differences found between configurations")
        return

    from rich.console import Console
    from rich.syntax import Syntax

    console = Console()

    click.echo("Differences found:")
    for key, changes in diff_result.items():
        if "old" in changes and "new" in changes:
            click.echo(f"\n{key}:")
            console.print(Syntax(f"- {changes['old']}", "python", theme="ansi_light"))
            console.print(Syntax(f"+ {changes['new']}", "python", theme="ansi_light"))
        elif "added" in changes:
            click.echo(f"\n{key} (added):")
            console.print(Syntax(f"+ {changes['added']}", "python", theme="ansi_light"))
        elif "removed" in changes:
            click.echo(f"\n{key} (removed):")
            console.print(Syntax(f"- {changes['removed']}", "python", theme="ansi_light"))


@config_group.command()
@click.option("--backup", is_flag=True, help="Create backup before importing")
@click.argument("import_path", type=click.Path(exists=True))
def import_config(import_path: str, backup: bool) -> None:
    """Import configuration from another file.

    Examples:

    \b
    # Import configuration from file
    auto-coder config import /path/to/config.toml

    \b
    # Import configuration with backup
    auto-coder config import /path/to/config.toml --backup
    """
    config_path = LLMBackendConfig.DEFAULT_CONFIG_FILE
    import_path_obj = Path(import_path)

    # Load current config
    current_config = LLMBackendConfig.load_from_file(config_path)

    # Create backup if requested
    if backup and config_path.exists():
        backup_path = config_path.with_suffix(config_path.suffix + ".backup")
        import shutil

        shutil.copy2(config_path, backup_path)
        click.echo(f"Backup created at {backup_path}")

    # Import configuration
    success = current_config.import_config(import_path_obj)

    if success:
        current_config.save_to_file()
        click.echo(f"Configuration imported successfully from {import_path}")

        # Validate after import
        validation_errors = current_config.validate_config()
        if validation_errors:
            click.echo("\nValidation errors after import:")
            for error in validation_errors:
                click.echo(f"  - {error}")
        else:
            click.echo("\n✓ Imported configuration is valid")
    else:
        click.echo("Failed to import configuration")
        sys.exit(1)


@config_group.command()
@click.argument("export_path", type=click.Path())
def export_config(export_path: str) -> None:
    """Export current configuration to another file.

    Examples:

    \b
    # Export current configuration to a file
    auto-coder config export /path/to/backup/config.toml
    """
    config_path = LLMBackendConfig.DEFAULT_CONFIG_FILE
    export_path_obj = Path(export_path)

    # Load current config
    current_config = LLMBackendConfig.load_from_file(config_path)

    # Export configuration
    success = current_config.export_config(export_path_obj)

    if success:
        click.echo(f"Configuration exported successfully to {export_path}")
    else:
        click.echo("Failed to export configuration")
        sys.exit(1)


@config_group.command()
def health() -> None:
    """Show configuration health and status.

    Examples:

    \b
    # Check configuration health
    auto-coder config health
    """
    config_path = LLMBackendConfig.DEFAULT_CONFIG_FILE

    click.echo("Configuration Health Check:")
    click.echo("=" * 30)

    # Check if config directory exists
    config_dir = LLMBackendConfig.DEFAULT_CONFIG_DIR
    if config_dir.exists():
        click.echo("✓ Configuration directory exists")
    else:
        click.echo("✗ Configuration directory does not exist")
        click.echo(f"  Expected: {config_dir}")

    # Check if config file exists
    if config_path.exists():
        click.echo("✓ Configuration file exists")

        # Try to load and validate
        try:
            config = LLMBackendConfig.load_from_file(config_path)
            validation_errors = config.validate_config()

            if validation_errors:
                click.echo("⚠ Configuration has validation errors:")
                for error in validation_errors:
                    click.echo(f"  - {error}")
            else:
                click.echo("✓ Configuration is valid")
        except Exception as e:
            click.echo(f"✗ Failed to load configuration: {e}")
    else:
        click.echo("✗ Configuration file does not exist")
        click.echo(f"  Expected: {config_path}")

    # Show environment variable overrides
    env_vars = [
        "GEMINI_API_KEY",
        "QWEN_API_KEY",
        "CLAUDE_API_KEY",
        "AUGGIE_API_KEY",
        "AUTO_CODER_CODEX_API_KEY",
        "AUTO_CODER_CODEX_MCP_API_KEY",
    ]

    env_overrides = []
    for var in env_vars:
        if os.getenv(var):
            env_overrides.append(var)

    if env_overrides:
        click.echo(f"✓ Environment variable overrides in use: {', '.join(env_overrides)}")
    else:
        click.echo("ℹ No environment variable overrides in use")

    # Check if config manager is initialized
    manager = LLMBackendConfigManager()
    if manager.config is not None:
        click.echo("✓ Configuration manager is initialized")
    else:
        click.echo("⚠ Configuration manager is not initialized")


# Add the config group to the CLI module's exports
__all__ = ["config_group"]

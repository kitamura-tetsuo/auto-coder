"""Configuration CLI commands for LLM backend management."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import click
import toml

from .llm_backend_config import LLMBackendConfig, get_llm_backend_config, initialize_llm_backend_config
from .logger_config import get_logger

logger = get_logger(__name__)


def get_config_path(config_file: Optional[str] = None) -> Path:
    """Get the path to the config file, creating directories if needed."""
    if config_file:
        config_path = Path(config_file).expanduser().resolve()
    else:
        # Create config directory if it doesn't exist
        config_dir = LLMBackendConfig.DEFAULT_CONFIG_DIR
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "llm_backend.toml"  # Use the proper TOML config file

    return config_path


@click.group()
def config_group() -> None:
    """Configuration management commands."""
    pass


@config_group.command()
@click.option("--file", "-f", "config_file", type=click.Path(exists=False), help="Path to config file (default: ~/.auto-coder/llm_backend.toml)")
def show(config_file: Optional[str]) -> None:
    """Display current configuration."""
    config_path = get_config_path(config_file)

    # Initialize the config manager to load the configuration
    initialize_llm_backend_config(config_path)
    config = get_llm_backend_config()

    # Convert to dict and display
    config_dict = config.to_dict()
    click.echo(json.dumps(config_dict, indent=2))


@config_group.command()
@click.option("--file", "-f", "config_file", type=click.Path(exists=False), help="Path to config file (default: ~/.auto-coder/llm_backend.toml)")
def edit(config_file: Optional[str]) -> None:
    """Open configuration file in default editor."""
    config_path = get_config_path(config_file)

    # Create the file if it doesn't exist with a basic structure
    if not config_path.exists():
        # Initialize to ensure the config and its directory exist
        initialize_llm_backend_config(config_path)
        config = get_llm_backend_config()
        config.save_default_config(config_path)

    # Determine the editor to use
    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "nano"))

    try:
        subprocess.run([editor, str(config_path)], check=True)
    except subprocess.CalledProcessError as e:
        click.echo(f"Error opening editor: {e}")
    except FileNotFoundError:
        click.echo(f"Editor '{editor}' not found. Please set your EDITOR or VISUAL environment variable.")


@config_group.command()
@click.option("--file", "-f", "config_file", type=click.Path(exists=False), help="Path to config file (default: ~/.auto-coder/llm_backend.toml)")
@click.argument("key", required=True)
@click.argument("value", required=True)
def set(config_file: Optional[str], key: str, value: str) -> None:
    """Set a configuration value."""
    config_path = get_config_path(config_file)

    # Initialize the config manager to load the configuration
    initialize_llm_backend_config(config_path)
    config = get_llm_backend_config()

    # Parse the key to allow nested properties (e.g., backends.codex.model)
    keys = key.split(".")

    if len(keys) < 2:
        click.echo(f"Invalid key format. Expected format: backend.property, e.g., 'codex.model'")
        return

    backend_name = keys[0]
    property_name = keys[1]

    # Validate backend name
    valid_backends = ["codex", "codex_mcp", "gemini", "qwen", "claude", "auggie"]
    if backend_name not in valid_backends:
        click.echo(f"Invalid backend name. Valid options: {', '.join(valid_backends)}")
        return

    # Get the backend config object
    backend_config = config.get_backend_config(backend_name)
    if backend_config is None:
        click.echo(f"Backend '{backend_name}' not found in configuration")
        return

    # Convert value to appropriate type
    converted_value: Any = value
    if value.lower() in ("true", "false"):
        converted_value = value.lower() == "true"
    elif value.isdigit():
        converted_value = int(value)
    else:
        try:
            converted_value = float(value)
        except ValueError:
            # Keep as string
            converted_value = value

    # Set the property on the backend config
    if not hasattr(backend_config, property_name):
        click.echo(f"Property '{property_name}' not found for backend '{backend_name}'")
        return

    setattr(backend_config, property_name, converted_value)

    # Save the configuration
    try:
        config.save_to_file(config_path)
        click.echo(f"Set {key} = {converted_value}")
    except Exception as e:
        click.echo(f"Error writing configuration file: {e}")


@config_group.command()
@click.option("--file", "-f", "config_file", type=click.Path(exists=False), help="Path to config file (default: ~/.auto-coder/llm_backend.toml)")
@click.argument("key", required=True)
def get(config_file: Optional[str], key: str) -> None:
    """Get a configuration value."""
    config_path = get_config_path(config_file)

    # Initialize the config manager to load the configuration
    initialize_llm_backend_config(config_path)
    config = get_llm_backend_config()

    # Parse the key to allow nested properties (e.g., backends.codex.model)
    keys = key.split(".")

    if len(keys) < 2:
        click.echo(f"Invalid key format. Expected format: backend.property, e.g., 'codex.model'")
        return

    backend_name = keys[0]
    property_name = keys[1]

    # Validate backend name
    valid_backends = ["codex", "codex_mcp", "gemini", "qwen", "claude", "auggie"]
    if backend_name not in valid_backends:
        click.echo(f"Invalid backend name. Valid options: {', '.join(valid_backends)}")
        return

    # Get the backend config object
    backend_config = config.get_backend_config(backend_name)
    if backend_config is None:
        click.echo(f"Backend '{backend_name}' not found in configuration")
        return

    # Get the property value
    if not hasattr(backend_config, property_name):
        click.echo(f"Property '{property_name}' not found for backend '{backend_name}'")
        return

    value = getattr(backend_config, property_name)

    # Print the value
    if isinstance(value, (dict, list)):
        click.echo(json.dumps(value, indent=2))
    else:
        click.echo(value)


@config_group.command()
@click.option("--file", "-f", "config_file", type=click.Path(exists=False), help="Path to config file (default: ~/.auto-coder/llm_backend.toml)")
def reset(config_file: Optional[str]) -> None:
    """Reset configuration to default values."""
    config_path = get_config_path(config_file)

    # Create a new default configuration and save it
    default_config = LLMBackendConfig()
    try:
        default_config.save_default_config(config_path)
        click.echo("Configuration reset to default values")
    except Exception as e:
        click.echo(f"Error resetting configuration: {e}")


@config_group.command()
@click.option("--file", "-f", "config_file", type=click.Path(exists=False), help="Path to config file (default: ~/.auto-coder/llm_backend.toml)")
def validate(config_file: Optional[str]) -> None:
    """Validate configuration file format."""
    config_path = get_config_path(config_file)

    if not config_path.exists():
        click.echo(f"Configuration file does not exist: {config_path}")
        return

    try:
        # Load the configuration to validate it
        config = LLMBackendConfig.load_from_file(config_path)

        # Run validation checks
        errors = config.validate_config()

        if errors:
            click.echo("Configuration validation errors found:")
            for error in errors:
                click.echo(f"  - {error}")
        else:
            click.echo("Configuration is valid")
    except Exception as e:
        click.echo(f"Error validating configuration: {e}")

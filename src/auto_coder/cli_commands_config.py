"""Configuration CLI commands for LLM backend management."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import click

from .llm_backend_config import LLMBackendConfiguration, get_llm_config
from .logger_config import get_logger

logger = get_logger(__name__)


def get_config_path(config_file: Optional[str] = None) -> Path:
    """Get the path to the config file, creating directories if needed."""
    if config_file:
        config_path = Path(config_file).expanduser().resolve()
    else:
        # Use the default config path
        config_path = Path.home() / ".auto-coder" / "llm_config.toml"

    return config_path


@click.group()
def config_group() -> None:
    """Configuration management commands."""
    pass


@config_group.command()
@click.option("--file", "-f", "config_file", type=click.Path(exists=False), help="Path to config file (default: ~/.auto-coder/llm_config.toml)")
def show(config_file: Optional[str]) -> None:
    """Display current configuration."""
    config_path = get_config_path(config_file)

    # Load the configuration
    if config_path.exists():
        config = LLMBackendConfiguration.load_from_file(str(config_path))
    else:
        # Create a default configuration if it doesn't exist
        config = LLMBackendConfiguration()

    # Convert to dict and display
    config_dict = config_to_dict(config)
    click.echo(json.dumps(config_dict, indent=2))


@config_group.command()
@click.option("--file", "-f", "config_file", type=click.Path(exists=False), help="Path to config file (default: ~/.auto-coder/llm_config.toml)")
def edit(config_file: Optional[str]) -> None:
    """Open configuration file in default editor."""
    config_path = get_config_path(config_file)

    # Create the directory if it doesn't exist
    config_path.parent.mkdir(parents=True, exist_ok=True)

    # Create the file if it doesn't exist with a basic structure
    if not config_path.exists():
        # Initialize with a default configuration
        config = LLMBackendConfiguration()
        config.save_to_file(str(config_path))

    # Determine the editor to use
    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "nano"))

    try:
        subprocess.run([editor, str(config_path)], check=True)
    except subprocess.CalledProcessError as e:
        click.echo(f"Error opening editor: {e}")
    except FileNotFoundError:
        click.echo(f"Editor '{editor}' not found. Please set your EDITOR or VISUAL environment variable.")


@config_group.command()
@click.option("--file", "-f", "config_file", type=click.Path(exists=False), help="Path to config file (default: ~/.auto-coder/llm_config.toml)")
@click.argument("key", required=True)
@click.argument("value", required=True)
def set(config_file: Optional[str], key: str, value: str) -> None:
    """Set a configuration value."""
    config_path = get_config_path(config_file)

    # Load or create the configuration
    if config_path.exists():
        config = LLMBackendConfiguration.load_from_file(str(config_path))
    else:
        config = LLMBackendConfiguration()

    # Parse the key to allow nested properties (e.g., backends.gemini.model)
    keys = key.split(".")

    if len(keys) < 2:
        click.echo(f"Invalid key format. Expected format: backend.property, e.g., 'gemini.model'")
        return

    backend_name = keys[0]
    property_name = keys[1]

    # Validate backend name
    if backend_name not in config.backends:
        click.echo(f"Backend '{backend_name}' not found in configuration")
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
        config.save_to_file(str(config_path))
        click.echo(f"Set {key} = {converted_value}")
    except Exception as e:
        click.echo(f"Error writing configuration file: {e}")


@config_group.command()
@click.option("--file", "-f", "config_file", type=click.Path(exists=False), help="Path to config file (default: ~/.auto-coder/llm_config.toml)")
@click.argument("key", required=True)
def get(config_file: Optional[str], key: str) -> None:
    """Get a configuration value."""
    config_path = get_config_path(config_file)

    # Load the configuration
    if config_path.exists():
        config = LLMBackendConfiguration.load_from_file(str(config_path))
    else:
        click.echo("Configuration file does not exist")
        return

    # Parse the key to allow nested properties (e.g., backends.gemini.model)
    keys = key.split(".")

    if len(keys) < 2:
        click.echo(f"Invalid key format. Expected format: backend.property, e.g., 'gemini.model'")
        return

    backend_name = keys[0]
    property_name = keys[1]

    # Validate backend name
    if backend_name not in config.backends:
        click.echo(f"Backend '{backend_name}' not found in configuration")
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
    elif value is not None:
        click.echo(value)
    else:
        click.echo("")


@config_group.command()
@click.option("--file", "-f", "config_file", type=click.Path(exists=False), help="Path to config file (default: ~/.auto-coder/llm_config.toml)")
def reset(config_file: Optional[str]) -> None:
    """Reset configuration to default values."""
    config_path = get_config_path(config_file)

    # Create a new default configuration and save it
    default_config = LLMBackendConfiguration()
    try:
        default_config.save_to_file(str(config_path))
        click.echo("Configuration reset to default values")
    except Exception as e:
        click.echo(f"Error resetting configuration: {e}")


@config_group.command()
@click.option("--file", "-f", "config_file", type=click.Path(exists=False), help="Path to config file (default: ~/.auto-coder/llm_config.toml)")
def validate(config_file: Optional[str]) -> None:
    """Validate configuration file format."""
    config_path = get_config_path(config_file)

    if not config_path.exists():
        click.echo(f"Configuration file does not exist")
        return

    try:
        # Load the configuration to validate it
        config = LLMBackendConfiguration.load_from_file(str(config_path))

        # Run validation checks
        errors = config_validate(config)

        if errors:
            click.echo("Configuration validation errors found:")
            for error in errors:
                click.echo(f"  - {error}")
        else:
            click.echo("Configuration is valid")
    except Exception as e:
        click.echo(f"Error validating configuration: {e}")


def config_to_dict(config: LLMBackendConfiguration) -> Dict[str, Any]:
    """Convert LLMBackendConfiguration to a dictionary."""
    result: Dict[str, Any] = {
        "backend": {
            "order": config.backend_order,
            "default": config.default_backend,
        },
        "backends": {},
    }

    for name, backend_config in config.backends.items():
        result["backends"][name] = {
            "enabled": backend_config.enabled,
            "model": backend_config.model,
            "api_key": backend_config.api_key,
            "base_url": backend_config.base_url,
            "temperature": backend_config.temperature,
            "timeout": backend_config.timeout,
            "max_retries": backend_config.max_retries,
            "openai_api_key": backend_config.openai_api_key,
            "openai_base_url": backend_config.openai_base_url,
            "extra_args": backend_config.extra_args,
        }

    result["message_backend"] = {
        "order": config.message_backend_order,
        "default": config.message_default_backend,
    }

    return result


def config_validate(config: LLMBackendConfiguration) -> List[str]:
    """Validate configuration and return list of errors."""
    errors: List[str] = []

    # Check each backend
    for name, backend_config in config.backends.items():
        # Type validation is handled by the dataclass definition
        # Additional validations can be added here if needed

        pass

    return errors

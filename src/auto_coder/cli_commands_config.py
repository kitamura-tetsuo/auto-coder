"""Configuration CLI commands for LLM backend management."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import click

from .llm_backend_config import LLMBackendConfig
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
        config_path = config_dir / LLMBackendConfig.DEFAULT_CONFIG_FILE

    # Create the file with a basic structure if it doesn't exist
    if not config_path.exists():
        config_path.write_text('{"backends": {"default": "codex"}}\n', encoding="utf-8")

    return config_path


@click.group()
def config_group() -> None:
    """Configuration management commands."""
    pass


@config_group.command()
@click.option("--file", "-f", "config_file", type=click.Path(exists=False), help="Path to config file (default: ~/.auto-coder/config.json)")
def show(config_file: Optional[str]) -> None:
    """Display current configuration."""
    config_path = get_config_path(config_file)

    if not config_path.exists():
        click.echo(f"Configuration file not found: {config_path}")
        return

    try:
        content = config_path.read_text(encoding="utf-8")
        config = json.loads(content)
        click.echo(json.dumps(config, indent=2))
    except json.JSONDecodeError as e:
        click.echo(f"Error parsing configuration file: {e}")
    except Exception as e:
        click.echo(f"Error reading configuration file: {e}")


@config_group.command()
@click.option("--file", "-f", "config_file", type=click.Path(exists=False), help="Path to config file (default: ~/.auto-coder/config.json)")
def edit(config_file: Optional[str]) -> None:
    """Open configuration file in default editor."""
    config_path = get_config_path(config_file)

    # Create the file if it doesn't exist with a basic structure
    if not config_path.exists():
        default_config = {"backends": {"default": "codex", "order": ["codex", "gemini", "qwen", "auggie", "claude"]}, "models": {}, "credentials": {}}
        config_path.write_text(json.dumps(default_config, indent=2), encoding="utf-8")

    # Determine the editor to use
    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "nano"))

    try:
        subprocess.run([editor, str(config_path)], check=True)
    except subprocess.CalledProcessError as e:
        click.echo(f"Error opening editor: {e}")
    except FileNotFoundError:
        click.echo(f"Editor '{editor}' not found. Please set your EDITOR or VISUAL environment variable.")


@config_group.command()
@click.option("--file", "-f", "config_file", type=click.Path(exists=False), help="Path to config file (default: ~/.auto-coder/config.json)")
@click.argument("key", required=True)
@click.argument("value", required=True)
def set(config_file: Optional[str], key: str, value: str) -> None:
    """Set a configuration value."""
    config_path = get_config_path(config_file)

    # Read existing config
    if config_path.exists():
        try:
            content = config_path.read_text(encoding="utf-8")
            config = json.loads(content)
        except json.JSONDecodeError as e:
            click.echo(f"Error parsing configuration file: {e}")
            return
    else:
        config = {}

    # Parse the key to allow nested properties (e.g., backends.default)
    keys = key.split(".")
    current = config

    # Navigate to the parent of the target key
    for k in keys[:-1]:
        if k not in current or not isinstance(current[k], dict):
            current[k] = {}
        current = current[k]

    # Convert value to appropriate type (try bool, int, float, json, string)
    converted_value: Any = value
    if value.lower() in ("true", "false"):
        converted_value = value.lower() == "true"
    else:
        try:
            # Try integer
            converted_value = int(value)
        except ValueError:
            try:
                # Try float
                converted_value = float(value)
            except ValueError:
                try:
                    # Try JSON
                    converted_value = json.loads(value)
                except json.JSONDecodeError:
                    # Keep as string
                    converted_value = value

    # Set the value
    current[keys[-1]] = converted_value

    # Write back to file
    try:
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        click.echo(f"Set {key} = {converted_value}")
    except Exception as e:
        click.echo(f"Error writing configuration file: {e}")


@config_group.command()
@click.option("--file", "-f", "config_file", type=click.Path(exists=False), help="Path to config file (default: ~/.auto-coder/config.json)")
@click.argument("key", required=True)
def get(config_file: Optional[str], key: str) -> None:
    """Get a configuration value."""
    config_path = get_config_path(config_file)

    if not config_path.exists():
        click.echo(f"Configuration file not found: {config_path}")
        return

    try:
        content = config_path.read_text(encoding="utf-8")
        config = json.loads(content)
    except json.JSONDecodeError as e:
        click.echo(f"Error parsing configuration file: {e}")
        return

    # Parse the key to allow nested properties (e.g., backends.default)
    keys = key.split(".")
    current = config

    # Navigate to the target key
    for k in keys:
        if isinstance(current, dict) and k in current:
            current = current[k]
        else:
            click.echo(f"Key '{key}' not found in configuration")
            return

    # Print the value
    if isinstance(current, (dict, list)):
        click.echo(json.dumps(current, indent=2))
    else:
        click.echo(current)


@config_group.command()
@click.option("--file", "-f", "config_file", type=click.Path(exists=False), help="Path to config file (default: ~/.auto-coder/config.json)")
def reset(config_file: Optional[str]) -> None:
    """Reset configuration to default values."""
    config_path = get_config_path(config_file)

    default_config = {"backends": {"default": "codex", "order": ["codex", "gemini", "qwen", "auggie", "claude"]}, "models": {}, "credentials": {}}

    try:
        config_path.write_text(json.dumps(default_config, indent=2), encoding="utf-8")
        click.echo("Configuration reset to default values")
    except Exception as e:
        click.echo(f"Error resetting configuration: {e}")


@config_group.command()
def validate() -> None:
    """Validate configuration file format."""
    config_path = get_config_path()

    if not config_path.exists():
        click.echo(f"Configuration file does not exist: {config_path}")
        return

    try:
        content = config_path.read_text(encoding="utf-8")
        config = json.loads(content)

        # Perform basic validation
        errors = []

        if not isinstance(config, dict):
            errors.append("Configuration must be a JSON object")

        if "backends" in config:
            backends = config["backends"]
            if not isinstance(backends, dict):
                errors.append("'backends' must be a JSON object")
            elif "default" in backends and not isinstance(backends["default"], str):
                errors.append("'backends.default' must be a string")

        if errors:
            click.echo("Configuration validation errors found:")
            for error in errors:
                click.echo(f"  - {error}")
        else:
            click.echo("Configuration is valid")
    except json.JSONDecodeError as e:
        click.echo(f"Invalid JSON format: {e}")
    except Exception as e:
        click.echo(f"Error validating configuration: {e}")

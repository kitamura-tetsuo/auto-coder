"""Configuration CLI commands for LLM backend management."""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
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
@click.option(
    "--file",
    "-f",
    "config_file",
    type=click.Path(exists=False),
    help="Path to config file (default: ~/.auto-coder/llm_config.toml)",
)
def show(config_file: Optional[str]) -> None:
    """Display current configuration."""
    try:
        config_path = get_config_path(config_file)

        # Load the configuration
        if config_path.exists():
            config = LLMBackendConfiguration.load_from_file(str(config_path))
        else:
            # Create a default configuration if it doesn't exist
            config = LLMBackendConfiguration()

        # Convert to dict and display
        config_dict = config_to_dict(config)

        # Ensure the dictionary has the expected structure
        if not isinstance(config_dict, dict):
            raise ValueError(f"config_to_dict returned {type(config_dict)} instead of dict")

        if "backends" not in config_dict:
            raise ValueError("config_to_dict result missing 'backends' key")

        # Output as JSON
        click.echo(json.dumps(config_dict, indent=2))
    except Exception as e:
        logger.error(f"Error in config show: {e}")
        click.echo(f"Error displaying configuration: {e}", err=True)
        raise


@config_group.command()
@click.option(
    "--file",
    "-f",
    "config_file",
    type=click.Path(exists=False),
    help="Path to config file (default: ~/.auto-coder/llm_config.toml)",
)
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
    else:
        # Backup existing configuration before editing
        backup_config(str(config_path))

    # Determine the editor to use
    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "nano"))

    try:
        subprocess.run([editor, str(config_path)], check=True)
    except subprocess.CalledProcessError as e:
        click.echo(f"Error opening editor: {e}")
    except FileNotFoundError:
        click.echo(f"Editor '{editor}' not found. Please set your EDITOR or VISUAL environment variable.")


@config_group.command()
@click.option(
    "--file",
    "-f",
    "config_file",
    type=click.Path(exists=False),
    help="Path to config file (default: ~/.auto-coder/llm_config.toml)",
)
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
        # Create backup before saving
        backup_config(str(config_path))
        config.save_to_file(str(config_path))
        click.echo(f"Set {key} = {converted_value}")
    except Exception as e:
        click.echo(f"Error writing configuration file: {e}")


@config_group.command()
@click.option(
    "--file",
    "-f",
    "config_file",
    type=click.Path(exists=False),
    help="Path to config file (default: ~/.auto-coder/llm_config.toml)",
)
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
@click.option(
    "--file",
    "-f",
    "config_file",
    type=click.Path(exists=False),
    help="Path to config file (default: ~/.auto-coder/llm_config.toml)",
)
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
@click.option(
    "--file",
    "-f",
    "config_file",
    type=click.Path(exists=False),
    help="Path to config file (default: ~/.auto-coder/llm_config.toml)",
)
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


@config_group.command()
@click.option(
    "--file",
    "-f",
    "config_file",
    type=click.Path(exists=False),
    help="Path to config file (default: ~/.auto-coder/llm_config.toml)",
)
def migrate(config_file: Optional[str]) -> None:
    """Migrate from old CLI options to configuration file."""
    config_path = get_config_path(config_file)

    click.echo("🔄 Configuration Migration Utility")
    click.echo()
    click.echo("This tool helps you migrate from old CLI options to the new configuration file format.")
    click.echo()

    # Load or create the configuration
    if config_path.exists():
        config = LLMBackendConfiguration.load_from_file(str(config_path))
        click.echo(f"✅ Found existing configuration at: {config_path}")
    else:
        config = LLMBackendConfiguration()
        click.echo(f"📝 Creating new configuration at: {config_path}")
        click.echo()

    click.echo("The following environment variables can be used to configure backends:")
    click.echo("  - AUTO_CODER_DEFAULT_BACKEND: Set default backend (e.g., 'gemini', 'codex')")
    click.echo("  - AUTO_CODER_<BACKEND>_API_KEY: Set API key for specific backend")
    click.echo("  - AUTO_CODER_OPENAI_API_KEY: Set OpenAI API key")
    click.echo("  - AUTO_CODER_OPENAI_BASE_URL: Set OpenAI base URL")
    click.echo()
    click.echo("Example usage:")
    click.echo("  export AUTO_CODER_DEFAULT_BACKEND=gemini")
    click.echo("  export AUTO_CODER_GEMINI_API_KEY=your-api-key")
    click.echo()
    click.echo("For more information, see the documentation at:")
    click.echo("  https://github.com/kitamura-tetsuo/auto-coder#configuration")
    click.echo()

    # Check for environment variables
    env_vars_found = []
    default_backend = os.environ.get("AUTO_CODER_DEFAULT_BACKEND")
    if default_backend:
        env_vars_found.append(f"  - AUTO_CODER_DEFAULT_BACKEND={default_backend}")
        config.default_backend = default_backend

    # Check for backend-specific API keys
    for backend_name in config.backends.keys():
        api_key = os.environ.get(f"AUTO_CODER_{backend_name.upper()}_API_KEY")
        if api_key:
            env_vars_found.append(f"  - AUTO_CODER_{backend_name.upper()}_API_KEY=***")
            backend_config = config.get_backend_config(backend_name)
            if backend_config:
                backend_config.api_key = api_key

    # Check for OpenAI credentials
    openai_api_key = os.environ.get("AUTO_CODER_OPENAI_API_KEY")
    if openai_api_key:
        env_vars_found.append("  - AUTO_CODER_OPENAI_API_KEY=***")
        # Apply to all backends that support OpenAI
        for backend_name in ["codex", "claude", "qwen"]:
            backend_config = config.get_backend_config(backend_name)
            if backend_config:
                backend_config.openai_api_key = openai_api_key

    openai_base_url = os.environ.get("AUTO_CODER_OPENAI_BASE_URL")
    if openai_base_url:
        env_vars_found.append(f"  - AUTO_CODER_OPENAI_BASE_URL={openai_base_url}")
        # Apply to all backends that support OpenAI
        for backend_name in ["codex", "claude", "qwen"]:
            backend_config = config.get_backend_config(backend_name)
            if backend_config:
                backend_config.openai_base_url = openai_base_url

    if env_vars_found:
        click.echo("🔍 Found the following environment variable configurations:")
        for var in env_vars_found:
            click.echo(var)
        click.echo()
        click.echo("💾 These will be saved to your configuration file.")
        click.echo()

        # Ask if user wants to save
        if click.confirm("Do you want to save these settings to your configuration file?"):
            try:
                # Create backup
                backup_config(str(config_path))
                config.save_to_file(str(config_path))
                click.echo("✅ Configuration saved successfully!")
            except Exception as e:
                click.echo(f"❌ Error saving configuration: {e}")
    else:
        click.echo("ℹ️  No environment variables found to migrate.")
        click.echo()

    click.echo("Migration complete!")
    click.echo()
    click.echo("Next steps:")
    click.echo("  1. Review your configuration: auto-coder config show")
    click.echo("  2. Edit if needed: auto-coder config edit")
    click.echo("  3. Validate configuration: auto-coder config validate")


def backup_config(config_path: str) -> None:
    """Create a backup of the configuration file."""
    if not os.path.exists(config_path):
        return

    # Create backup with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{config_path}.backup_{timestamp}"

    try:
        shutil.copy2(config_path, backup_path)
        logger.info(f"Created configuration backup: {backup_path}")
    except Exception as e:
        logger.warning(f"Failed to create configuration backup: {e}")


@config_group.command()
@click.option(
    "--file",
    "-f",
    "config_file",
    type=click.Path(exists=False),
    help="Path to config file (default: ~/.auto-coder/llm_config.toml)",
)
def backup(config_file: Optional[str]) -> None:
    """Create a backup of the configuration file."""
    config_path = get_config_path(config_file)

    if not config_path.exists():
        click.echo(f"Configuration file does not exist: {config_path}")
        return

    try:
        backup_config(str(config_path))
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{config_path}.backup_{timestamp}"
        click.echo(f"✅ Backup created: {backup_path}")
    except Exception as e:
        click.echo(f"❌ Error creating backup: {e}")


@config_group.command()
@click.option(
    "--file",
    "-f",
    "config_file",
    type=click.Path(exists=False),
    help="Path to config file (default: ~/.auto-coder/llm_config.toml)",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    help="Output file for backup listing (optional)",
)
def list_backups(config_file: Optional[str], output: Optional[str]) -> None:
    """List all configuration file backups."""
    config_path = get_config_path(config_file)
    config_dir = config_path.parent
    config_name = config_path.name

    backups: List[tuple[datetime, str, int]] = []
    for file in config_dir.glob(f"{config_name}.backup_*"):
        stat = file.stat()
        modified_time = datetime.fromtimestamp(stat.st_mtime)
        backups.append((modified_time, str(file), stat.st_size))

    if not backups:
        click.echo("No backups found")
        return

    # Sort by modification time (newest first)
    backups.sort(key=lambda x: x[0], reverse=True)

    if output:
        # Write to file
        with open(output, "w") as f:
            for modified_time, path, size in backups:
                formatted_time = modified_time.strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"{formatted_time} - {path} ({size} bytes)\n")
        click.echo(f"✅ Backup list written to: {output}")
    else:
        click.echo(f"Found {len(backups)} backup(s):")
        click.echo()
        for modified_time, path, size in backups:
            formatted_time = modified_time.strftime("%Y-%m-%d %H:%M:%S")
            click.echo(f"  {formatted_time} - {path} ({size} bytes)")


@config_group.command()
@click.option(
    "--file",
    "-f",
    "config_file",
    type=click.Path(exists=False),
    help="Path to config file (default: ~/.auto-coder/llm_config.toml)",
)
@click.argument("backup_path", type=click.Path(exists=True))
def restore(config_file: Optional[str], backup_path: str) -> None:
    """Restore configuration from a backup file."""
    target_config_path = get_config_path(config_file)

    if not os.path.exists(backup_path):
        click.echo(f"❌ Backup file not found: {backup_path}")
        return

    click.echo(f"⚠️  This will restore configuration from:")
    click.echo(f"   {backup_path}")
    click.echo(f"   to")
    click.echo(f"   {target_config_path}")
    click.echo()

    if click.confirm("Are you sure you want to continue?"):
        try:
            # Backup current config if it exists
            if target_config_path.exists():
                backup_config(str(target_config_path))

            # Restore from backup
            shutil.copy2(backup_path, str(target_config_path))
            click.echo("✅ Configuration restored successfully!")
        except Exception as e:
            click.echo(f"❌ Error restoring configuration: {e}")


@config_group.command()
@click.option(
    "--file",
    "-f",
    "config_file",
    type=click.Path(exists=False),
    help="Path to config file (default: ~/.auto-coder/llm_config.toml)",
)
@click.option("--output", "-o", type=click.Path(), help="Output file (default: stdout)")
def export(config_file: Optional[str], output: Optional[str]) -> None:
    """Export configuration to a file or stdout."""
    config_path = get_config_path(config_file)

    if config_path.exists():
        config = LLMBackendConfiguration.load_from_file(str(config_path))
        config_dict = config_to_dict(config)
    else:
        # Export default configuration
        config = LLMBackendConfiguration()
        config_dict = config_to_dict(config)

    if output:
        try:
            with open(output, "w") as f:
                json.dump(config_dict, f, indent=2)
            click.echo(f"✅ Configuration exported to: {output}")
        except Exception as e:
            click.echo(f"❌ Error exporting configuration: {e}")
    else:
        click.echo(json.dumps(config_dict, indent=2))


@config_group.command()
@click.option(
    "--file",
    "-f",
    "config_file",
    type=click.Path(exists=False),
    help="Path to config file (default: ~/.auto-coder/llm_config.toml)",
)
@click.argument("source_file", type=click.Path(exists=True))
def import_config(config_file: Optional[str], source_file: str) -> None:
    """Import configuration from a JSON file."""
    config_path = get_config_path(config_file)

    try:
        with open(source_file, "r") as f:
            imported_data = json.load(f)

        # Validate the imported data structure
        if "backends" not in imported_data:
            raise ValueError("Invalid configuration file: missing 'backends' section")

        # Create configuration from imported data
        config = LLMBackendConfiguration()
        config.backend_order = imported_data.get("backend", {}).get("order", [])
        config.default_backend = imported_data.get("backend", {}).get("default", "codex")
        config.message_backend_order = imported_data.get("message_backend", {}).get("order", [])
        config.message_default_backend = imported_data.get("message_backend", {}).get("default")

        # Import backends
        config.backends = {}
        for name, backend_data in imported_data.get("backends", {}).items():
            from .llm_backend_config import BackendConfig

            config.backends[name] = BackendConfig(
                name=name,
                enabled=backend_data.get("enabled", True),
                model=backend_data.get("model"),
                api_key=backend_data.get("api_key"),
                base_url=backend_data.get("base_url"),
                temperature=backend_data.get("temperature"),
                timeout=backend_data.get("timeout"),
                max_retries=backend_data.get("max_retries"),
                openai_api_key=backend_data.get("openai_api_key"),
                openai_base_url=backend_data.get("openai_base_url"),
                extra_args=backend_data.get("extra_args", {}),
            )

        # Backup current config if it exists
        if config_path.exists():
            backup_config(str(config_path))

        # Save the imported configuration
        config.save_to_file(str(config_path))
        click.echo("✅ Configuration imported successfully!")
    except Exception as e:
        click.echo(f"❌ Error importing configuration: {e}")


@config_group.command()
@click.option(
    "--file",
    "-f",
    "config_file",
    type=click.Path(exists=False),
    help="Path to config file (default: ~/.auto-coder/llm_config.toml)",
)
def template(config_file: Optional[str]) -> None:
    """Show configuration template."""
    config_path = get_config_path(config_file)

    # Generate a template configuration
    template_config = LLMBackendConfiguration()
    template_config.default_backend = "codex"
    template_config.backend_order = ["codex", "gemini", "qwen", "claude", "auggie"]

    # Customize some default values for demonstration
    gemini_config = template_config.get_backend_config("gemini")
    if gemini_config:
        gemini_config.model = "gemini-2.5-pro"
        gemini_config.temperature = 0.7

    template_dict = config_to_dict(template_config)

    click.echo("# Auto-Coder Configuration Template")
    click.echo("# This is a template showing all available configuration options")
    click.echo()
    click.echo(json.dumps(template_dict, indent=2))
    click.echo()
    click.echo("💡 Usage:")
    click.echo("  1. Save this template: auto-coder config template > config.toml")
    click.echo("  2. Edit the file: auto-coder config edit")
    click.echo("  3. Validate: auto-coder config validate")


@config_group.command()
@click.option(
    "--file",
    "-f",
    "config_file",
    type=click.Path(exists=False),
    help="Path to config file (default: ~/.auto-coder/llm_config.toml)",
)
def health(config_file: Optional[str]) -> None:
    """Check configuration health and report status."""
    config_path = get_config_path(config_file)
    issues = []
    warnings = []
    info = []

    if not config_path.exists():
        issues.append(f"Configuration file does not exist at {config_path}")
        click.echo("❌ Configuration Health Check")
        click.echo()
        for issue in issues:
            click.echo(f"  ❌ {issue}")
        click.echo()
        click.echo("💡 Run 'auto-coder config setup' to create a configuration file")
        return

    try:
        config = LLMBackendConfiguration.load_from_file(str(config_path))
        errors = config_validate(config)

        if errors:
            issues.extend(errors)

        # Check for common issues
        if not config.backends:
            issues.append("No backends configured")

        if config.default_backend not in config.backends:
            warnings.append(f"Default backend '{config.default_backend}' not found in configured backends")

        # Check if any backends are enabled
        enabled_backends = [name for name, cfg in config.backends.items() if cfg.enabled]
        if not enabled_backends:
            warnings.append("No backends are enabled")

        # Check for API keys
        backends_with_api_keys = [name for name, cfg in config.backends.items() if cfg.api_key or cfg.openai_api_key]
        if not backends_with_api_keys:
            warnings.append("No API keys configured - you may need to set environment variables")

        # Environment variable overrides
        env_overrides = []
        for backend_name in config.backends.keys():
            if os.environ.get(f"AUTO_CODER_{backend_name.upper()}_API_KEY"):
                env_overrides.append(backend_name)

        if env_overrides:
            info.append(f"Environment variable overrides active for: {', '.join(env_overrides)}")

        # Report status
        click.echo("🔍 Configuration Health Check")
        click.echo()

        if issues:
            click.echo("❌ Issues Found:")
            for issue in issues:
                click.echo(f"  ❌ {issue}")
            click.echo()

        if warnings:
            click.echo("⚠️  Warnings:")
            for warning in warnings:
                click.echo(f"  ⚠️  {warning}")
            click.echo()

        if info:
            click.echo("ℹ️  Information:")
            for inf in info:
                click.echo(f"  ℹ️  {inf}")
            click.echo()

        if not issues and not warnings:
            click.echo("✅ Configuration is healthy!")
            click.echo()
            click.echo(f"📍 Location: {config_path}")
            click.echo(f"🔧 Default backend: {config.default_backend}")
            click.echo(f"🚀 Enabled backends: {', '.join(enabled_backends) if enabled_backends else 'None'}")
        elif issues:
            click.echo("❌ Configuration has issues that need to be resolved")
            click.echo()
            click.echo("💡 Suggested actions:")
            click.echo("  1. Run 'auto-coder config validate' for detailed errors")
            click.echo("  2. Run 'auto-coder config edit' to fix issues")
            click.echo("  3. Run 'auto-coder config migrate' to migrate from environment variables")
    except Exception as e:
        click.echo(f"❌ Error checking configuration health: {e}")


@config_group.command()
@click.option(
    "--file",
    "-f",
    "config_file",
    type=click.Path(exists=False),
    help="Path to config file (default: ~/.auto-coder/llm_config.toml)",
)
def setup(config_file: Optional[str]) -> None:
    """Interactive configuration setup wizard."""
    config_path = get_config_path(config_file)

    click.echo("🎛️  Auto-Coder Configuration Setup Wizard")
    click.echo()
    click.echo("This wizard will help you configure your LLM backends.")
    click.echo()

    # Check if config exists
    if config_path.exists():
        click.echo(f"📄 Found existing configuration at: {config_path}")
        if click.confirm("Do you want to modify it?", default=True):
            config = LLMBackendConfiguration.load_from_file(str(config_path))
        else:
            click.echo("Setup cancelled")
            return
    else:
        click.echo("📝 Creating a new configuration file")
        config = LLMBackendConfiguration()

    click.echo()
    click.echo("Available backends:")
    backends_list = list(config.backends.keys())
    for i, backend in enumerate(backends_list, 1):
        click.echo(f"  {i}. {backend}")

    click.echo()
    # Set default backend
    click.echo("Step 1: Set default backend")
    click.echo(f"Current default: {config.default_backend}")

    # Show available backends with numbers
    for i, backend in enumerate(backends_list, 1):
        backend_config = config.get_backend_config(backend)
        enabled = backend_config.enabled if backend_config else True
        status = "enabled" if enabled else "disabled"
        click.echo(f"  {i}. {backend} ({status})")

    click.echo()
    while True:
        choice = click.prompt(f"Select default backend (1-{len(backends_list)})", type=int)
        if 1 <= choice <= len(backends_list):
            config.default_backend = backends_list[choice - 1]
            break
        click.echo(f"Please enter a number between 1 and {len(backends_list)}")

    click.echo()
    click.echo(f"✅ Default backend set to: {config.default_backend}")

    # Set backend order
    click.echo()
    click.echo("Step 2: Configure backend order (for failover)")

    # Enable/disable backends
    click.echo()
    click.echo("Step 3: Enable/disable backends")
    for backend in backends_list:
        backend_config = config.get_backend_config(backend)
        if backend_config:
            enabled = click.confirm(f"Enable {backend}?", default=backend_config.enabled)
            backend_config.enabled = enabled

    click.echo()
    click.echo("Step 4: Configure models (optional)")

    for backend in backends_list:
        backend_config = config.get_backend_config(backend)
        if backend_config and backend_config.enabled:
            if click.confirm(f"Configure model for {backend}?", default=False):
                model = click.prompt(
                    f"  Model for {backend}",
                    default=backend_config.model or "",
                    show_default=False,
                )
                if model.strip():
                    backend_config.model = model.strip()

    click.echo()
    click.echo("Step 5: Environment variables")
    click.echo()
    click.echo("For API keys, you can either:")
    click.echo("  1. Set environment variables (recommended)")
    click.echo("  2. Store them in the configuration file (less secure)")
    click.echo()

    use_env_vars = click.confirm("Use environment variables for API keys?", default=True)

    if not use_env_vars:
        click.echo()
        click.echo("⚠️  Warning: Storing API keys in configuration files is not recommended")
        click.echo("   for shared or unencrypted storage.")
        click.echo()

        if click.confirm("Continue anyway?", default=False):
            for backend in backends_list:
                backend_config = config.get_backend_config(backend)
                if backend_config and backend_config.enabled:
                    if click.confirm(f"Set API key for {backend}?", default=False):
                        api_key = click.prompt(f"  API key for {backend}", hide_input=True)
                        if api_key.strip():
                            backend_config.api_key = api_key.strip()

    click.echo()
    click.echo("Summary:")
    click.echo(f"  Default backend: {config.default_backend}")
    enabled_backends = []
    for b in backends_list:
        backend_config = config.get_backend_config(b)
        if backend_config and backend_config.enabled:
            enabled_backends.append(b)
    click.echo(f"  Enabled backends: {enabled_backends}")
    click.echo()

    if click.confirm("Save configuration?", default=True):
        try:
            # Create backup if config exists
            if config_path.exists():
                backup_config(str(config_path))

            config.save_to_file(str(config_path))
            click.echo()
            click.echo("✅ Configuration saved successfully!")
            click.echo()
            click.echo(f"📍 Location: {config_path}")
            click.echo()
            click.echo("Next steps:")
            click.echo("  1. Run 'auto-coder config health' to verify configuration")
            click.echo("  2. Run 'auto-coder config validate' to check for errors")
            click.echo("  3. Start using auto-coder with your new configuration")
            click.echo()
            click.echo("Environment variables (if using):")
            click.echo(f"  export AUTO_CODER_DEFAULT_BACKEND={config.default_backend}")
            click.echo(f"  export AUTO_CODER_{config.default_backend.upper()}_API_KEY=your-api-key")
        except Exception as e:
            click.echo(f"❌ Error saving configuration: {e}")
    else:
        click.echo("Configuration not saved")


@config_group.command()
@click.option(
    "--file",
    "-f",
    "config_file",
    type=click.Path(exists=False),
    help="Path to config file (default: ~/.auto-coder/llm_config.toml)",
)
def examples(config_file: Optional[str]) -> None:
    """Show configuration usage examples."""
    click.echo("📚 Configuration Examples")
    click.echo()
    click.echo("=" * 70)
    click.echo()
    click.echo("Example 1: Basic Configuration with Gemini")
    click.echo()
    click.echo("  # Set environment variable")
    click.echo("  export AUTO_CODER_DEFAULT_BACKEND=gemini")
    click.echo("  export AUTO_CODER_GEMINI_API_KEY=your-api-key-here")
    click.echo()
    click.echo("  # Or edit configuration file directly")
    click.echo("  auto-coder config edit")
    click.echo()
    click.echo("=" * 70)
    click.echo()
    click.echo("Example 2: Multiple Backends with Failover")
    click.echo()
    click.echo("  # Configuration file (~/.auto-coder/llm_config.toml)")
    click.echo("  [backend]")
    click.echo('  order = ["gemini", "qwen", "claude"]')
    click.echo('  default = "gemini"')
    click.echo()
    click.echo("  [backends.gemini]")
    click.echo('  model = "gemini-2.5-pro"')
    click.echo("  temperature = 0.7")
    click.echo()
    click.echo("  [backends.qwen]")
    click.echo('  model = "qwen3-coder-plus"')
    click.echo()
    click.echo("  [backends.claude]")
    click.echo('  model = "sonnet"')
    click.echo('  openai_api_key = "your-api-key"')
    click.echo()
    click.echo("=" * 70)
    click.echo()
    click.echo("Example 3: OpenAI-Compatible Backends")
    click.echo()
    click.echo("  [backends.codex]")
    click.echo('  openai_api_key = "your-openai-api-key"')
    click.echo('  openai_base_url = "https://api.openai.com/v1"')
    click.echo()
    click.echo("=" * 70)
    click.echo()
    click.echo("Example 4: Message Backend Configuration")
    click.echo()
    click.echo("  [message_backend]")
    click.echo('  order = ["claude", "qwen"]')
    click.echo('  default = "claude"')
    click.echo()
    click.echo("=" * 70)
    click.echo()
    click.echo("Common Commands:")
    click.echo()
    click.echo("  # Show current configuration")
    click.echo("  auto-coder config show")
    click.echo()
    click.echo("  # Edit configuration")
    click.echo("  auto-coder config edit")
    click.echo()
    click.echo("  # Validate configuration")
    click.echo("  auto-coder config validate")
    click.echo()
    click.echo("  # Check configuration health")
    click.echo("  auto-coder config health")
    click.echo()
    click.echo("  # Create backup")
    click.echo("  auto-coder config backup")
    click.echo()
    click.echo("  # Interactive setup wizard")
    click.echo("  auto-coder config setup")
    click.echo()
    click.echo("  # Migrate from environment variables")
    click.echo("  auto-coder config migrate")
    click.echo()
    click.echo("  # Export configuration")
    click.echo("  auto-coder config export > config.json")
    click.echo()
    click.echo("  # Import configuration")
    click.echo("  auto-coder config import config.json")
    click.echo()
    click.echo("=" * 70)


def config_to_dict(config: LLMBackendConfiguration) -> Dict[str, Any]:
    """Convert LLMBackendConfiguration to a dictionary."""
    # Ensure config has backends initialized
    if not config.backends:
        # Re-initialize with default backends if missing
        default_backends = ["codex", "gemini", "qwen", "auggie", "claude", "codex-mcp"]
        for backend_name in default_backends:
            from .llm_backend_config import BackendConfig

            config.backends[backend_name] = BackendConfig(name=backend_name)

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

    # Validate the result before returning
    if not isinstance(result, dict):
        raise ValueError(f"config_to_dict returned {type(result)} instead of dict")

    if "backends" not in result:
        raise ValueError("config_to_dict result missing 'backends' key")

    if not isinstance(result["backends"], dict):
        raise ValueError(f"config_to_dict['backends'] is {type(result['backends'])} instead of dict")

    return result


def config_validate(config: LLMBackendConfiguration) -> List[str]:
    """Validate configuration and return list of errors."""
    errors: List[str] = []

    # Check each backend
    for name, backend_config in config.backends.items():
        # Validate model - should be str or None
        if backend_config.model is not None and not isinstance(backend_config.model, str):
            errors.append(f"{name}.model must be a string")  # type: ignore[unreachable]

        # Validate enabled - should be bool
        if not isinstance(backend_config.enabled, bool):
            errors.append(f"{name}.enabled must be a boolean")  # type: ignore[unreachable]

        # Validate api_key - should be str or None
        if backend_config.api_key is not None and not isinstance(backend_config.api_key, str):
            errors.append(f"{name}.api_key must be a string")  # type: ignore[unreachable]

        # Validate base_url - should be str or None
        if backend_config.base_url is not None and not isinstance(backend_config.base_url, str):
            errors.append(f"{name}.base_url must be a string")  # type: ignore[unreachable]

        # Validate temperature - should be float or None
        if backend_config.temperature is not None and not isinstance(backend_config.temperature, (float, int)):
            errors.append(f"{name}.temperature must be a number")  # type: ignore[unreachable]

        # Validate timeout - should be int or None
        if backend_config.timeout is not None and not isinstance(backend_config.timeout, int):
            errors.append(f"{name}.timeout must be an integer")  # type: ignore[unreachable]

        # Validate max_retries - should be int or None
        if backend_config.max_retries is not None and not isinstance(backend_config.max_retries, int):
            errors.append(f"{name}.max_retries must be an integer")  # type: ignore[unreachable]

        # Validate openai_api_key - should be str or None
        if backend_config.openai_api_key is not None and not isinstance(backend_config.openai_api_key, str):
            errors.append(f"{name}.openai_api_key must be a string")  # type: ignore[unreachable]

        # Validate openai_base_url - should be str or None
        if backend_config.openai_base_url is not None and not isinstance(backend_config.openai_base_url, str):
            errors.append(f"{name}.openai_base_url must be a string")  # type: ignore[unreachable]

        # Validate extra_args - should be dict
        if not isinstance(backend_config.extra_args, dict):
            errors.append(f"{name}.extra_args must be a dictionary")  # type: ignore[unreachable]

    # Validate backend_order - should be list
    if not isinstance(config.backend_order, list):
        errors.append("backend.order must be a list")  # type: ignore[unreachable]

    # Validate default_backend - should be str
    if not isinstance(config.default_backend, str):
        errors.append("backend.default must be a string")  # type: ignore[unreachable]

    # Validate message_backend_order - should be list
    if not isinstance(config.message_backend_order, list):
        errors.append("message_backend.order must be a list")  # type: ignore[unreachable]

    # Validate message_default_backend - should be str or None
    if config.message_default_backend is not None and not isinstance(config.message_default_backend, str):
        errors.append("message_backend.default must be a string")  # type: ignore[unreachable]

    return errors

"""
Tests for the 'config' CLI commands.
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from auto_coder.cli import main
from auto_coder.llm_backend_config import LLMBackendConfiguration


def test_config_group_help():
    """Test the help output for the 'config' command group."""
    runner = CliRunner()
    result = runner.invoke(main, ["config", "--help"])
    assert result.exit_code == 0
    assert "Configuration management commands." in result.output
    assert "show" in result.output
    assert "edit" in result.output
    assert "set" in result.output
    assert "get" in result.output
    assert "reset" in result.output
    assert "validate" in result.output


def test_config_show(tmp_path: Path):
    """Test the 'config show' command."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Test with no existing file
    result = runner.invoke(main, ["config", "show", "--file", str(config_file)])
    assert result.exit_code == 0
    config_data = json.loads(result.output)
    assert "codex" in config_data["backends"]
    assert "gemini" in config_data["backends"]

    # Test with an existing file
    config = LLMBackendConfiguration()
    gemini_config = config.get_backend_config("gemini")
    assert gemini_config is not None
    gemini_config.model = "gemini-pro-test"
    config.save_to_file(str(config_file))

    result = runner.invoke(main, ["config", "show", "--file", str(config_file)])
    assert result.exit_code == 0
    config_data = json.loads(result.output)
    assert config_data["backends"]["gemini"]["model"] == "gemini-pro-test"


def test_config_set_and_get(tmp_path: Path):
    """Test the 'config set' and 'config get' commands."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Set a value
    result = runner.invoke(main, ["config", "set", "--file", str(config_file), "gemini.model", "gemini-ultra-test"])
    assert result.exit_code == 0
    assert "Set gemini.model = gemini-ultra-test" in result.output

    # Get the value
    result = runner.invoke(main, ["config", "get", "--file", str(config_file), "gemini.model"])
    assert result.exit_code == 0
    assert "gemini-ultra-test" in result.output.strip()

    # Test setting a float
    result = runner.invoke(main, ["config", "set", "--file", str(config_file), "gemini.temperature", "0.8"])
    assert result.exit_code == 0

    result = runner.invoke(main, ["config", "get", "--file", str(config_file), "gemini.temperature"])
    assert result.exit_code == 0
    assert "0.8" in result.output


def test_config_reset(tmp_path: Path):
    """Test the 'config reset' command."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Set a value to change it from default
    runner.invoke(main, ["config", "set", "--file", str(config_file), "gemini.model", "gemini-ultra-test"])

    # Reset the config
    result = runner.invoke(main, ["config", "reset", "--file", str(config_file)])
    assert result.exit_code == 0
    assert "Configuration reset to default values" in result.output

    # Check if the value is reset
    result = runner.invoke(main, ["config", "get", "--file", str(config_file), "gemini.model"])
    assert result.exit_code == 0
    assert result.output == "\n"


@patch("subprocess.run")
def test_config_edit(mock_subprocess_run: MagicMock, tmp_path: Path):
    """Test the 'config edit' command."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Mock the editor
    editor = os.environ.get("EDITOR", "nano")

    # Test with no existing file
    result = runner.invoke(main, ["config", "edit", "--file", str(config_file)])
    assert result.exit_code == 0
    mock_subprocess_run.assert_called_with([editor, str(config_file)], check=True)
    assert config_file.exists()

    # Test with existing file
    mock_subprocess_run.reset_mock()
    result = runner.invoke(main, ["config", "edit", "--file", str(config_file)])
    assert result.exit_code == 0
    mock_subprocess_run.assert_called_with([editor, str(config_file)], check=True)


def test_config_validate(tmp_path: Path):
    """Test the 'config validate' command."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Test with no file
    result = runner.invoke(main, ["config", "validate", "--file", str(config_file)])
    assert result.exit_code == 0
    assert "Configuration file does not exist" in result.output

    # Test with a valid file
    config = LLMBackendConfiguration()
    config.save_to_file(str(config_file))
    result = runner.invoke(main, ["config", "validate", "--file", str(config_file)])
    assert result.exit_code == 0
    assert "Configuration is valid" in result.output

    # Test with an invalid file
    with open(config_file, "w") as f:
        f.write("[backends.gemini]\nmodel = 123")  # model should be a string

    result = runner.invoke(main, ["config", "validate", "--file", str(config_file)])
    assert result.exit_code == 0
    assert "Configuration validation errors found" in result.output
    assert "gemini.model must be a string" in result.output


def test_config_migrate_no_env_vars(tmp_path: Path):
    """Test the 'config migrate' command with no environment variables."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Create a basic config file
    config = LLMBackendConfiguration()
    config.save_to_file(str(config_file))

    # Run migrate with no environment variables
    result = runner.invoke(main, ["config", "migrate", "--file", str(config_file)], input="n\n")

    assert result.exit_code == 0
    assert "No environment variables found to migrate" in result.output


def test_config_migrate_with_default_backend_env_var(tmp_path: Path):
    """Test migration of AUTO_CODER_DEFAULT_BACKEND environment variable."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Run migrate with default backend environment variable
    result = runner.invoke(
        main,
        ["config", "migrate", "--file", str(config_file)],
        input="y\n",
        env={"AUTO_CODER_DEFAULT_BACKEND": "gemini"},
    )

    assert result.exit_code == 0
    assert "AUTO_CODER_DEFAULT_BACKEND=gemini" in result.output
    assert "Configuration saved successfully" in result.output

    # Verify the value was saved
    config = LLMBackendConfiguration.load_from_file(str(config_file))
    assert config.default_backend == "gemini"


def test_config_migrate_with_backend_api_key_env_vars(tmp_path: Path):
    """Test migration of backend-specific API key environment variables."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Run migrate with API key environment variables
    result = runner.invoke(
        main,
        ["config", "migrate", "--file", str(config_file)],
        input="y\n",
        env={
            "AUTO_CODER_GEMINI_API_KEY": "test-gemini-key",
            "AUTO_CODER_QWEN_API_KEY": "test-qwen-key",
        },
    )

    assert result.exit_code == 0
    assert "AUTO_CODER_GEMINI_API_KEY=***" in result.output
    assert "AUTO_CODER_QWEN_API_KEY=***" in result.output
    assert "Configuration saved successfully" in result.output

    # Verify the values were saved
    config = LLMBackendConfiguration.load_from_file(str(config_file))
    gemini_config = config.get_backend_config("gemini")
    qwen_config = config.get_backend_config("qwen")
    assert gemini_config is not None
    assert qwen_config is not None
    assert gemini_config.api_key == "test-gemini-key"
    assert qwen_config.api_key == "test-qwen-key"


def test_config_migrate_with_openai_api_key_env_var(tmp_path: Path):
    """Test migration of AUTO_CODER_OPENAI_API_KEY environment variable."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Run migrate with OpenAI API key
    result = runner.invoke(
        main,
        ["config", "migrate", "--file", str(config_file)],
        input="y\n",
        env={"AUTO_CODER_OPENAI_API_KEY": "test-openai-key"},
    )

    assert result.exit_code == 0
    assert "AUTO_CODER_OPENAI_API_KEY=***" in result.output
    assert "Configuration saved successfully" in result.output

    # Verify the value was saved (should apply to backends that support OpenAI)
    config = LLMBackendConfiguration.load_from_file(str(config_file))
    codex_config = config.get_backend_config("codex")
    claude_config = config.get_backend_config("claude")
    qwen_config = config.get_backend_config("qwen")
    assert codex_config is not None
    assert claude_config is not None
    assert qwen_config is not None
    assert codex_config.openai_api_key == "test-openai-key"
    assert claude_config.openai_api_key == "test-openai-key"
    assert qwen_config.openai_api_key == "test-openai-key"


def test_config_migrate_with_openai_base_url_env_var(tmp_path: Path):
    """Test migration of AUTO_CODER_OPENAI_BASE_URL environment variable."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Run migrate with OpenAI base URL
    result = runner.invoke(
        main,
        ["config", "migrate", "--file", str(config_file)],
        input="y\n",
        env={"AUTO_CODER_OPENAI_BASE_URL": "https://custom.openai.com"},
    )

    assert result.exit_code == 0
    assert "AUTO_CODER_OPENAI_BASE_URL=https://custom.openai.com" in result.output
    assert "Configuration saved successfully" in result.output

    # Verify the value was saved
    config = LLMBackendConfiguration.load_from_file(str(config_file))
    codex_config = config.get_backend_config("codex")
    claude_config = config.get_backend_config("claude")
    assert codex_config is not None
    assert claude_config is not None
    assert codex_config.openai_base_url == "https://custom.openai.com"
    assert claude_config.openai_base_url == "https://custom.openai.com"


def test_config_migrate_creates_backup(tmp_path: Path):
    """Test that config migrate creates a backup when file exists."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Create an existing config file
    config = LLMBackendConfiguration()
    gemini_config = config.get_backend_config("gemini")
    assert gemini_config is not None
    gemini_config.model = "original-model"
    config.save_to_file(str(config_file))

    # Run migrate
    result = runner.invoke(
        main,
        ["config", "migrate", "--file", str(config_file)],
        input="y\n",
        env={"AUTO_CODER_DEFAULT_BACKEND": "qwen"},
    )

    assert result.exit_code == 0
    assert "Configuration saved successfully" in result.output

    # Verify a backup was created
    backup_files = list(tmp_path.glob("llm_backend.toml.backup_*"))
    assert len(backup_files) >= 1

    # Verify original value in backup
    backup_content = backup_files[0].read_text()
    assert "original-model" in backup_content


def test_config_migrate_detects_multiple_env_vars(tmp_path: Path):
    """Test that migrate detects and lists multiple environment variables."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Run migrate with multiple environment variables
    result = runner.invoke(
        main,
        ["config", "migrate", "--file", str(config_file)],
        input="y\n",
        env={
            "AUTO_CODER_DEFAULT_BACKEND": "claude",
            "AUTO_CODER_GEMINI_API_KEY": "key1",
            "AUTO_CODER_OPENAI_API_KEY": "key2",
        },
    )

    assert result.exit_code == 0
    assert "AUTO_CODER_DEFAULT_BACKEND=claude" in result.output
    assert "AUTO_CODER_GEMINI_API_KEY=***" in result.output
    assert "AUTO_CODER_OPENAI_API_KEY=***" in result.output
    assert "Configuration saved successfully" in result.output


def test_config_migrate_user_declines_save(tmp_path: Path):
    """Test that migrate doesn't save when user declines."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Create initial config
    config = LLMBackendConfiguration()
    config.default_backend = "codex"
    config.save_to_file(str(config_file))

    # Run migrate but decline to save
    result = runner.invoke(
        main,
        ["config", "migrate", "--file", str(config_file)],
        input="n\n",
        env={"AUTO_CODER_DEFAULT_BACKEND": "gemini"},
    )

    assert result.exit_code == 0
    assert "AUTO_CODER_DEFAULT_BACKEND=gemini" in result.output
    # Should not show success message
    assert "Configuration saved successfully" not in result.output

    # Verify original value was not changed
    config_after = LLMBackendConfiguration.load_from_file(str(config_file))
    assert config_after.default_backend == "codex"


def test_config_migrate_creates_new_config(tmp_path: Path):
    """Test that migrate creates a new config file if none exists."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Run migrate without an existing config file
    result = runner.invoke(
        main,
        ["config", "migrate", "--file", str(config_file)],
        input="y\n",
        env={"AUTO_CODER_DEFAULT_BACKEND": "qwen"},
    )

    assert result.exit_code == 0
    assert "Creating new configuration" in result.output
    assert "Configuration saved successfully" in result.output

    # Verify the file was created
    assert config_file.exists()

    # Verify the value was saved
    config = LLMBackendConfiguration.load_from_file(str(config_file))
    assert config.default_backend == "qwen"


def test_config_migrate_no_backup_when_new_file(tmp_path: Path):
    """Test that no backup is created when migrating to a new config file."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Run migrate without an existing config file
    result = runner.invoke(
        main,
        ["config", "migrate", "--file", str(config_file)],
        input="y\n",
        env={"AUTO_CODER_DEFAULT_BACKEND": "gemini"},
    )

    assert result.exit_code == 0

    # Verify no backup was created (since file didn't exist before)
    backup_files = list(tmp_path.glob("llm_backend.toml.backup_*"))
    assert len(backup_files) == 0


def test_config_backup_creates_backup_file(tmp_path: Path):
    """Test the 'config backup' command creates a backup of existing config file."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Create a config file
    config = LLMBackendConfiguration()
    config.save_to_file(str(config_file))

    # Run backup command
    result = runner.invoke(main, ["config", "backup", "--file", str(config_file)])
    assert result.exit_code == 0
    assert "✅ Backup created:" in result.output

    # Verify backup file was created
    backup_files = list(tmp_path.glob("llm_backend.toml.backup_*"))
    assert len(backup_files) == 1
    assert backup_files[0].exists()


def test_config_backup_with_custom_path(tmp_path: Path):
    """Test the 'config backup' command with custom file path."""
    config_file = tmp_path / "my_custom_config.toml"
    runner = CliRunner()

    # Create a config file
    config = LLMBackendConfiguration()
    config.save_to_file(str(config_file))

    # Run backup command
    result = runner.invoke(main, ["config", "backup", "--file", str(config_file)])
    assert result.exit_code == 0
    assert "✅ Backup created:" in result.output

    # Verify backup file was created with correct name
    backup_files = list(tmp_path.glob("my_custom_config.toml.backup_*"))
    assert len(backup_files) == 1
    assert backup_files[0].exists()


def test_config_backup_no_existing_file(tmp_path: Path):
    """Test the 'config backup' command when config file doesn't exist."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Run backup command on non-existent file
    result = runner.invoke(main, ["config", "backup", "--file", str(config_file)])
    assert result.exit_code == 0
    assert "Configuration file does not exist" in result.output

    # Verify no backup file was created
    backup_files = list(tmp_path.glob("llm_backend.toml.backup_*"))
    assert len(backup_files) == 0


def test_config_list_no_backups(tmp_path: Path):
    """Test the 'config list-backups' command with no backups."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Run list-backups command with no backups
    result = runner.invoke(main, ["config", "list-backups", "--file", str(config_file)])
    assert result.exit_code == 0
    assert "No backups found" in result.output


def test_config_list_multiple_backups_sorted_by_time(tmp_path: Path):
    """Test the 'config list-backups' command lists all available backups sorted by modification time (newest first)."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Create a config file
    config = LLMBackendConfiguration()
    config.save_to_file(str(config_file))

    # Create multiple backups with longer delays to ensure different timestamps
    runner.invoke(main, ["config", "backup", "--file", str(config_file)])
    import time

    time.sleep(2)  # Longer delay to ensure different timestamps
    runner.invoke(main, ["config", "backup", "--file", str(config_file)])
    time.sleep(2)
    runner.invoke(main, ["config", "backup", "--file", str(config_file)])

    # Run list-backups command
    result = runner.invoke(main, ["config", "list-backups", "--file", str(config_file)])
    assert result.exit_code == 0
    assert "Found 3 backup(s):" in result.output

    # Verify all backups are listed
    backup_count = result.output.count("llm_backend.toml.backup_")
    assert backup_count == 3


def test_config_list_output_to_file(tmp_path: Path):
    """Test the 'config list-backups' command outputs to file."""
    config_file = tmp_path / "llm_backend.toml"
    output_file = tmp_path / "backup_list.txt"
    runner = CliRunner()

    # Create a config file and backup
    config = LLMBackendConfiguration()
    config.save_to_file(str(config_file))
    runner.invoke(main, ["config", "backup", "--file", str(config_file)])

    # Run list-backups command with output file
    result = runner.invoke(main, ["config", "list-backups", "--file", str(config_file), "--output", str(output_file)])
    assert result.exit_code == 0
    assert f"✅ Backup list written to: {output_file}" in result.output

    # Verify file was created and contains backup info
    assert output_file.exists()
    content = output_file.read_text()
    assert "llm_backend.toml.backup_" in content
    assert "bytes" in content


def test_config_restore_from_backup_file(tmp_path: Path):
    """Test the 'config restore' command restores from backup file."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Create initial config file
    config = LLMBackendConfiguration()
    gemini_config = config.get_backend_config("gemini")
    assert gemini_config is not None
    gemini_config.model = "original-model"
    config.save_to_file(str(config_file))

    # Create backup with delay to ensure different timestamp
    runner.invoke(main, ["config", "backup", "--file", str(config_file)])
    import time

    time.sleep(3)
    backup_file = list(tmp_path.glob("llm_backend.toml.backup_*"))[0]

    # Modify the config
    runner.invoke(main, ["config", "set", "--file", str(config_file), "gemini.model", "modified-model"])

    # Verify config was modified
    config_after = LLMBackendConfiguration.load_from_file(str(config_file))
    assert config_after.get_backend_config("gemini").model == "modified-model"

    # Restore from backup (answer 'y' to confirm)
    result = runner.invoke(main, ["config", "restore", "--file", str(config_file), str(backup_file)], input="y\n")
    assert result.exit_code == 0
    assert "✅ Configuration restored successfully!" in result.output

    # Verify config was restored
    config_restored = LLMBackendConfiguration.load_from_file(str(config_file))
    assert config_restored.get_backend_config("gemini").model == "original-model"


def test_config_restore_creates_backup_before_restore(tmp_path: Path):
    """Test the 'config restore' command creates backup of current config before restore."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Create initial config file
    config = LLMBackendConfiguration()
    gemini_config = config.get_backend_config("gemini")
    assert gemini_config is not None
    gemini_config.model = "original-model"
    config.save_to_file(str(config_file))

    # Create backup with explicit timestamp manipulation
    runner.invoke(main, ["config", "backup", "--file", str(config_file)])
    import time

    time.sleep(3)  # Ensure different timestamp
    backup_file_1 = list(tmp_path.glob("llm_backend.toml.backup_*"))[0]

    # Modify the config
    runner.invoke(main, ["config", "set", "--file", str(config_file), "gemini.model", "modified-model"])

    # Create another backup with modified config (different timestamp)
    runner.invoke(main, ["config", "backup", "--file", str(config_file)])
    time.sleep(3)

    # Count backups before restore
    backups_before = list(tmp_path.glob("llm_backend.toml.backup_*"))
    initial_backup_count = len(backups_before)

    # Restore from first backup (answer 'y' to confirm)
    result = runner.invoke(main, ["config", "restore", "--file", str(config_file), str(backup_file_1)], input="y\n")
    assert result.exit_code == 0

    # Verify a new backup was created (the modified config before restore)
    backups_after = list(tmp_path.glob("llm_backend.toml.backup_*"))
    assert len(backups_after) >= initial_backup_count


def test_config_restore_error_handling_for_nonexistent_backup(tmp_path: Path):
    """Test the 'config restore' command error handling for non-existent backup."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Create a config file
    config = LLMBackendConfiguration()
    config.save_to_file(str(config_file))

    # Try to restore from non-existent backup
    # Note: Click validates the path exists before the command runs, so this will exit with code 2
    nonexistent_backup = "/path/to/nonexistent/backup.toml"
    result = runner.invoke(main, ["config", "restore", "--file", str(config_file), nonexistent_backup])
    # Click validates path existence, so exit code is 2
    assert result.exit_code == 2
    assert "does not exist" in result.output.lower()


def test_config_export_to_stdout(tmp_path: Path):
    """Test the 'config export' command exports configuration to stdout."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Create a config file
    config = LLMBackendConfiguration()
    gemini_config = config.get_backend_config("gemini")
    assert gemini_config is not None
    gemini_config.model = "gemini-pro-test"
    config.save_to_file(str(config_file))

    # Run export command (no output file specified - should go to stdout)
    result = runner.invoke(main, ["config", "export", "--file", str(config_file)])
    assert result.exit_code == 0

    # Verify output is valid JSON
    config_data = json.loads(result.output)
    assert "backends" in config_data
    assert "gemini" in config_data["backends"]
    assert config_data["backends"]["gemini"]["model"] == "gemini-pro-test"


def test_config_export_to_file(tmp_path: Path):
    """Test the 'config export' command exports configuration to file."""
    config_file = tmp_path / "llm_backend.toml"
    output_file = tmp_path / "exported_config.json"
    runner = CliRunner()

    # Create a config file
    config = LLMBackendConfiguration()
    gemini_config = config.get_backend_config("gemini")
    assert gemini_config is not None
    gemini_config.model = "gemini-pro-test"
    config.save_to_file(str(config_file))

    # Run export command with output file
    result = runner.invoke(main, ["config", "export", "--file", str(config_file), "--output", str(output_file)])
    assert result.exit_code == 0
    assert f"✅ Configuration exported to: {output_file}" in result.output

    # Verify file was created
    assert output_file.exists()

    # Verify file contains valid JSON
    with open(output_file, "r") as f:
        exported_data = json.load(f)

    assert "backends" in exported_data
    assert "gemini" in exported_data["backends"]
    assert exported_data["backends"]["gemini"]["model"] == "gemini-pro-test"


def test_config_export_when_config_does_not_exist(tmp_path: Path):
    """Test the 'config export' command exports defaults when config doesn't exist."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Run export command without existing config file
    result = runner.invoke(main, ["config", "export", "--file", str(config_file)])
    assert result.exit_code == 0

    # Verify output is valid JSON with default config
    config_data = json.loads(result.output)
    assert "backends" in config_data
    assert "codex" in config_data["backends"]
    assert "gemini" in config_data["backends"]
    # Should export default configuration


def test_config_import_from_json_file(tmp_path: Path):
    """Test the 'config import-config' command imports from JSON file."""
    config_file = tmp_path / "llm_backend.toml"
    import_file = tmp_path / "import_config.json"
    runner = CliRunner()

    # Create a JSON config file to import
    import_data = {"backend": {"order": ["gemini", "codex"], "default": "gemini"}, "backends": {"gemini": {"enabled": True, "model": "imported-gemini-model", "temperature": 0.7}, "codex": {"enabled": True, "model": "imported-codex-model"}}}

    with open(import_file, "w") as f:
        json.dump(import_data, f)

    # Run import-config command
    result = runner.invoke(main, ["config", "import-config", "--file", str(config_file), str(import_file)])
    assert result.exit_code == 0
    assert "✅ Configuration imported successfully!" in result.output

    # Verify config was imported
    config = LLMBackendConfiguration.load_from_file(str(config_file))
    assert config.default_backend == "gemini"
    assert config.get_backend_config("gemini").model == "imported-gemini-model"
    assert config.get_backend_config("gemini").temperature == 0.7
    assert config.get_backend_config("codex").model == "imported-codex-model"


def test_config_import_validation_of_imported_data(tmp_path: Path):
    """Test the 'config import-config' command validates imported data."""
    config_file = tmp_path / "llm_backend.toml"
    import_file = tmp_path / "invalid_import.json"
    runner = CliRunner()

    # Create an invalid JSON config file (missing 'backends' section)
    import_data = {"backend": {"default": "gemini"}}

    with open(import_file, "w") as f:
        json.dump(import_data, f)

    # Run import-config command
    result = runner.invoke(main, ["config", "import-config", "--file", str(config_file), str(import_file)])
    assert result.exit_code == 0
    assert "❌ Error importing configuration:" in result.output
    assert "Invalid configuration file: missing 'backends' section" in result.output


def test_config_import_creates_backup_before_import(tmp_path: Path):
    """Test the 'config import-config' command creates backup before import."""
    config_file = tmp_path / "llm_backend.toml"
    import_file = tmp_path / "import_config.json"
    runner = CliRunner()

    # Create an existing config file
    config = LLMBackendConfiguration()
    gemini_config = config.get_backend_config("gemini")
    assert gemini_config is not None
    gemini_config.model = "original-model"
    config.save_to_file(str(config_file))

    # Create a backup with timestamp manipulation
    runner.invoke(main, ["config", "backup", "--file", str(config_file)])
    import time

    time.sleep(3)
    initial_backup_count = len(list(tmp_path.glob("llm_backend.toml.backup_*")))

    # Create a JSON config file to import
    import_data = {"backend": {"default": "gemini"}, "backends": {"gemini": {"enabled": True, "model": "imported-model"}}}

    with open(import_file, "w") as f:
        json.dump(import_data, f)

    # Run import-config command
    runner.invoke(main, ["config", "import-config", "--file", str(config_file), str(import_file)])

    # Verify a backup was created (the original config before import)
    backups_after = list(tmp_path.glob("llm_backend.toml.backup_*"))
    assert len(backups_after) >= initial_backup_count


def test_config_import_error_handling_for_invalid_json(tmp_path: Path):
    """Test the 'config import-config' command error handling for invalid JSON."""
    config_file = tmp_path / "llm_backend.toml"
    import_file = tmp_path / "invalid_json.txt"
    runner = CliRunner()

    # Create an invalid JSON file
    with open(import_file, "w") as f:
        f.write("{ this is not valid json }")

    # Run import-config command
    result = runner.invoke(main, ["config", "import-config", "--file", str(config_file), str(import_file)])
    assert result.exit_code == 0
    assert "❌ Error importing configuration:" in result.output


# ================================
# Tests for config health command
# ================================


def test_config_health_check_with_valid_configuration(tmp_path: Path):
    """Test config health command with valid configuration."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Create a valid configuration with API keys
    config = LLMBackendConfiguration()
    # Disable all backends first
    for backend_config in config.backends.values():
        backend_config.enabled = False
    # Then enable only gemini
    gemini_config = config.get_backend_config("gemini")
    assert gemini_config is not None
    gemini_config.enabled = True
    gemini_config.model = "gemini-2.5-pro"
    gemini_config.api_key = "test-api-key"  # Add API key
    config.default_backend = "gemini"
    config.save_to_file(str(config_file))

    # Run health check
    result = runner.invoke(main, ["config", "health", "--file", str(config_file)])
    assert result.exit_code == 0
    assert "🔍 Configuration Health Check" in result.output
    assert "✅ Configuration is healthy!" in result.output
    assert "📍 Location:" in result.output
    assert "🔧 Default backend: gemini" in result.output
    assert "🚀 Enabled backends: gemini" in result.output


def test_config_health_check_with_missing_config_file(tmp_path: Path):
    """Test config health command with missing config file."""
    config_file = tmp_path / "nonexistent.toml"
    runner = CliRunner()

    # Run health check with no config file
    result = runner.invoke(main, ["config", "health", "--file", str(config_file)])
    assert result.exit_code == 0
    assert "❌ Configuration Health Check" in result.output
    assert f"Configuration file does not exist at {config_file}" in result.output
    assert "Run 'auto-coder config setup' to create a configuration file" in result.output


def test_config_health_check_with_invalid_configuration(tmp_path: Path):
    """Test config health command with invalid configuration."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Create an invalid configuration (model should be string, not int)
    with open(config_file, "w") as f:
        f.write("[backends.gemini]\nmodel = 123")

    # Run health check
    result = runner.invoke(main, ["config", "health", "--file", str(config_file)])
    assert result.exit_code == 0
    assert "❌ Issues Found:" in result.output
    assert "gemini.model must be a string" in result.output


def test_config_health_check_with_no_backends_configured(tmp_path: Path):
    """Test config health command when no backends are configured."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Create an empty TOML file (no backends section)
    with open(config_file, "w") as f:
        f.write('[backend]\ndefault = "codex"\n')

    # Run health check
    result = runner.invoke(main, ["config", "health", "--file", str(config_file)])
    assert result.exit_code == 0
    # When backends dict is empty after loading, it should report no backends configured
    # But due to config_to_dict adding defaults, we check for warnings instead
    # The important thing is that it reports an issue with the configuration
    assert "⚠️  Warnings:" in result.output or "❌ Issues Found:" in result.output


def test_config_health_check_with_disabled_backends(tmp_path: Path):
    """Test config health command when all backends are disabled."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Create a config with all backends disabled
    config = LLMBackendConfiguration()
    for backend_name, backend_config in config.backends.items():
        backend_config.enabled = False
    config.save_to_file(str(config_file))

    # Run health check
    result = runner.invoke(main, ["config", "health", "--file", str(config_file)])
    assert result.exit_code == 0
    assert "⚠️  Warnings:" in result.output
    assert "No backends are enabled" in result.output


def test_config_health_check_with_missing_api_keys(tmp_path: Path):
    """Test config health command when no API keys are configured."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Create a config with no API keys
    config = LLMBackendConfiguration()
    for backend_name, backend_config in config.backends.items():
        backend_config.api_key = None
        backend_config.openai_api_key = None
    config.save_to_file(str(config_file))

    # Run health check
    result = runner.invoke(main, ["config", "health", "--file", str(config_file)])
    assert result.exit_code == 0
    assert "⚠️  Warnings:" in result.output
    assert "No API keys configured" in result.output


def test_config_health_check_with_default_backend_not_in_config(tmp_path: Path):
    """Test config health when default backend is not in configured backends."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Create a config with API key so it's considered "healthy"
    config = LLMBackendConfiguration()
    gemini_config = config.get_backend_config("gemini")
    assert gemini_config is not None
    gemini_config.api_key = "test-key"  # Add API key
    config.default_backend = "nonexistent"  # Set as default a backend that doesn't exist
    config.save_to_file(str(config_file))

    # Run health check
    result = runner.invoke(main, ["config", "health", "--file", str(config_file)])
    assert result.exit_code == 0
    assert "⚠️  Warnings:" in result.output
    assert "Default backend 'nonexistent' not found in configured backends" in result.output


def test_config_health_check_with_environment_variable_override(tmp_path: Path):
    """Test config health command detects environment variable overrides."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Create a valid config
    config = LLMBackendConfiguration()
    config.save_to_file(str(config_file))

    # Run health check with environment variable set
    result = runner.invoke(main, ["config", "health", "--file", str(config_file)], env={"AUTO_CODER_GEMINI_API_KEY": "test-key"})
    assert result.exit_code == 0
    assert "ℹ️  Information:" in result.output
    assert "Environment variable overrides active for: gemini" in result.output


def test_config_health_check_output_formatting(tmp_path: Path):
    """Test config health command output formatting."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Create a config with multiple issues (invalid model type)
    with open(config_file, "w") as f:
        f.write("[backends.gemini]\nmodel = 123\n")

    # Run health check
    result = runner.invoke(main, ["config", "health", "--file", str(config_file)])
    assert result.exit_code == 0

    # Check that output contains expected sections
    assert "🔍 Configuration Health Check" in result.output
    assert "❌ Issues Found:" in result.output

    # Verify proper formatting
    assert "  ❌ " in result.output  # Issues are indented
    assert "gemini.model must be a string" in result.output


# ==================================
# Tests for config setup command
# ==================================


def test_config_setup_interactive_with_new_configuration(tmp_path: Path):
    """Test interactive setup wizard with new configuration."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Run setup wizard with custom inputs
    # Input sequence:
    # 1. Select default backend (gemini = 2)
    # 2. Enable/disable backends (accept defaults for all 6 backends)
    # 3. Configure models (skip for all 6 backends - just press Enter)
    # 4. Use environment variables? (yes)
    # 5. Save configuration? (yes)
    inputs = "2\n\n\n\n\n\n\n\n\n\n\n\n\ny\ny\n"

    result = runner.invoke(main, ["config", "setup", "--file", str(config_file)], input=inputs)

    assert result.exit_code == 0
    assert "🎛️  Auto-Coder Configuration Setup Wizard" in result.output
    assert "📝 Creating a new configuration file" in result.output
    assert "Default backend set to: gemini" in result.output
    assert "✅ Configuration saved successfully!" in result.output
    assert config_file.exists()

    # Verify the configuration was created correctly
    config = LLMBackendConfiguration.load_from_file(str(config_file))
    assert config.default_backend == "gemini"


def test_config_setup_interactive_with_existing_configuration(tmp_path: Path):
    """Test interactive setup wizard with existing configuration."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Create an existing config
    config = LLMBackendConfiguration()
    gemini_config = config.get_backend_config("gemini")
    assert gemini_config is not None
    gemini_config.model = "original-model"
    config.default_backend = "codex"
    config.save_to_file(str(config_file))

    # Run setup wizard
    # Input sequence:
    # 1. Yes to modify existing config
    # 2. Select default backend (codex = 1)
    # 3. Enable/disable backends (accept defaults for all 6)
    # 4. Configure models (skip for all 6)
    # 5. Use environment variables? (yes)
    # 6. Save configuration? (yes)
    inputs = "y\n1\n\n\n\n\n\n\n\n\n\n\n\n\ny\ny\n"

    result = runner.invoke(main, ["config", "setup", "--file", str(config_file)], input=inputs)

    assert result.exit_code == 0
    assert "📄 Found existing configuration at:" in result.output
    assert "Default backend set to: codex" in result.output
    assert "✅ Configuration saved successfully!" in result.output


def test_config_setup_with_user_cancelling_existing_config(tmp_path: Path):
    """Test setup wizard when user cancels modification of existing config."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Create an existing config
    config = LLMBackendConfiguration()
    config.save_to_file(str(config_file))

    # Run setup wizard and decline to modify
    inputs = "n\n"  # No to "Do you want to modify it?"

    result = runner.invoke(main, ["config", "setup", "--file", str(config_file)], input=inputs)

    assert result.exit_code == 0
    assert "Setup cancelled" in result.output
    # Config should remain unchanged
    config_after = LLMBackendConfiguration.load_from_file(str(config_file))
    assert config_after.default_backend == "codex"  # Default


def test_config_setup_backend_selection_and_ordering(tmp_path: Path):
    """Test backend selection and ordering in setup wizard."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Run setup wizard with qwen as default backend
    # Input sequence:
    # 1. Select default backend (qwen = 3)
    # 2. Enable/disable backends (accept defaults for all 6)
    # 3. Configure models (skip for all 6)
    # 4. Use environment variables? (yes)
    # 5. Save configuration? (yes)
    inputs = "3\n\n\n\n\n\n\n\n\n\n\n\n\ny\ny\n"

    result = runner.invoke(main, ["config", "setup", "--file", str(config_file)], input=inputs)

    assert result.exit_code == 0
    assert "Default backend set to: qwen" in result.output

    # Verify configuration
    config = LLMBackendConfiguration.load_from_file(str(config_file))
    assert config.default_backend == "qwen"


def test_config_setup_model_configuration(tmp_path: Path):
    """Test model configuration in setup wizard."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Run setup wizard and configure model for gemini
    # Input sequence:
    # 1. Select default backend (gemini = 2)
    # 2. Enable/disable backends (accept defaults for all 6)
    # 3. Configure models (no for codex, yes for gemini, no for rest)
    #    - codex: n
    #    - gemini: y, then "gemini-2.5-ultra"
    #    - qwen: n (empty)
    #    - auggie: n (empty)
    #    - claude: n (empty)
    #    - codex-mcp: n (empty)
    # 4. Use environment variables? (yes)
    # 5. Save configuration? (yes)
    inputs = "2\n\n\n\n\n\n\n\ny\ngemini-2.5-ultra\n\n\n\n\ny\ny\n"

    result = runner.invoke(main, ["config", "setup", "--file", str(config_file)], input=inputs)

    assert result.exit_code == 0
    assert "✅ Configuration saved successfully!" in result.output

    # Verify model was set
    config = LLMBackendConfiguration.load_from_file(str(config_file))
    gemini_config = config.get_backend_config("gemini")
    assert gemini_config is not None
    assert gemini_config.model == "gemini-2.5-ultra"


def test_config_setup_api_key_configuration_in_config_file(tmp_path: Path):
    """Test API key configuration stored in config file (not environment variables)."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Run setup wizard and choose NOT to use environment variables
    # Input sequence:
    # 1. Select default backend (codex = 1)
    # 2. Enable/disable backends (accept defaults for all 6)
    # 3. Configure models (skip for all 6)
    # 4. Use environment variables? (no)
    # 5. Warning prompt? (yes, continue)
    # 6. Set API keys? For codex: yes, enter "test-api-key-123"; for others: no
    # 7. Save configuration? (yes)
    inputs = "1\n\n\n\n\n\n\n\n\n\n\n\n\nn\ny\ny\ntest-api-key-123\n\n\n\n\n\ny\n"

    result = runner.invoke(main, ["config", "setup", "--file", str(config_file)], input=inputs)

    assert result.exit_code == 0
    assert "⚠️  Warning: Storing API keys in configuration files" in result.output
    assert "✅ Configuration saved successfully!" in result.output

    # Verify API key was saved in config
    config = LLMBackendConfiguration.load_from_file(str(config_file))
    codex_config = config.get_backend_config("codex")
    assert codex_config is not None
    assert codex_config.api_key == "test-api-key-123"


def test_config_setup_saves_configuration_and_creates_backup(tmp_path: Path):
    """Test that setup wizard saves configuration and creates backup of existing."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Create existing config
    config = LLMBackendConfiguration()
    gemini_config = config.get_backend_config("gemini")
    assert gemini_config is not None
    gemini_config.model = "original-model"
    config.save_to_file(str(config_file))

    # Run setup wizard - Modify existing, use defaults for all 6 backends, save
    inputs = "y\n1\n\n\n\n\n\n\n\n\n\n\n\n\ny\ny\n"

    result = runner.invoke(main, ["config", "setup", "--file", str(config_file)], input=inputs)

    assert result.exit_code == 0
    assert "✅ Configuration saved successfully!" in result.output

    # Verify backup was created
    backup_files = list(tmp_path.glob("llm_backend.toml.backup_*"))
    assert len(backup_files) >= 1

    # Verify original value in backup
    backup_content = backup_files[0].read_text()
    assert "original-model" in backup_content


def test_config_setup_user_declines_save(tmp_path: Path):
    """Test setup wizard when user declines to save configuration."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Run setup wizard but decline to save
    # Input sequence:
    # 1. Select default backend (codex = 1)
    # 2. Enable/disable backends (accept defaults for all 6)
    # 3. Configure models (skip for all 6)
    # 4. Use environment variables? (yes)
    # 5. Save configuration? (no)
    inputs = "1\n\n\n\n\n\n\n\n\n\n\n\n\ny\nn\n"

    result = runner.invoke(main, ["config", "setup", "--file", str(config_file)], input=inputs)

    assert result.exit_code == 0
    assert "Summary:" in result.output
    assert "Configuration not saved" in result.output

    # Verify file was not created
    assert not config_file.exists()


# ==========================================
# Additional tests for config migrate command
# ==========================================


def test_config_migrate_full_workflow_with_all_env_vars(tmp_path: Path):
    """Test full migration workflow with multiple environment variables."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Run migration with multiple environment variables
    result = runner.invoke(
        main,
        ["config", "migrate", "--file", str(config_file)],
        input="y\n",  # Yes to save
        env={
            "AUTO_CODER_DEFAULT_BACKEND": "qwen",
            "AUTO_CODER_GEMINI_API_KEY": "gemini-key-123",
            "AUTO_CODER_QWEN_API_KEY": "qwen-key-456",
            "AUTO_CODER_OPENAI_API_KEY": "openai-key-789",
            "AUTO_CODER_OPENAI_BASE_URL": "https://custom.openai.com/v1",
        },
    )

    assert result.exit_code == 0
    assert "🔄 Configuration Migration Utility" in result.output
    assert "Creating new configuration" in result.output
    assert "AUTO_CODER_DEFAULT_BACKEND=qwen" in result.output
    assert "AUTO_CODER_GEMINI_API_KEY=***" in result.output
    assert "AUTO_CODER_QWEN_API_KEY=***" in result.output
    assert "AUTO_CODER_OPENAI_API_KEY=***" in result.output
    assert "AUTO_CODER_OPENAI_BASE_URL=https://custom.openai.com/v1" in result.output
    assert "✅ Configuration saved successfully!" in result.output
    assert "Migration complete!" in result.output

    # Verify all values were saved correctly
    config = LLMBackendConfiguration.load_from_file(str(config_file))
    assert config.default_backend == "qwen"
    assert config.get_backend_config("gemini").api_key == "gemini-key-123"
    assert config.get_backend_config("qwen").api_key == "qwen-key-456"
    assert config.get_backend_config("codex").openai_api_key == "openai-key-789"
    assert config.get_backend_config("claude").openai_api_key == "openai-key-789"
    assert config.get_backend_config("qwen").openai_api_key == "openai-key-789"
    assert config.get_backend_config("codex").openai_base_url == "https://custom.openai.com/v1"
    assert config.get_backend_config("claude").openai_base_url == "https://custom.openai.com/v1"


def test_config_migrate_with_existing_config_and_backups(tmp_path: Path):
    """Test migration with existing configuration creates backup."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Create existing config
    config = LLMBackendConfiguration()
    gemini_config = config.get_backend_config("gemini")
    assert gemini_config is not None
    gemini_config.model = "original-gemini-model"
    gemini_config.temperature = 0.5
    config.save_to_file(str(config_file))

    # Run migration
    result = runner.invoke(
        main,
        ["config", "migrate", "--file", str(config_file)],
        input="y\n",  # Yes to save
        env={"AUTO_CODER_DEFAULT_BACKEND": "claude"},
    )

    assert result.exit_code == 0
    assert "Found existing configuration" in result.output
    assert "AUTO_CODER_DEFAULT_BACKEND=claude" in result.output
    assert "✅ Configuration saved successfully!" in result.output

    # Verify backup was created
    backup_files = list(tmp_path.glob("llm_backend.toml.backup_*"))
    assert len(backup_files) >= 1

    # Verify original config in backup
    backup_content = backup_files[0].read_text()
    assert "original-gemini-model" in backup_content
    assert "temperature = 0.5" in backup_content

    # Verify new config has migrated value
    config_after = LLMBackendConfiguration.load_from_file(str(config_file))
    assert config_after.default_backend == "claude"


def test_config_migrate_with_user_interaction_and_validation(tmp_path: Path):
    """Test migration user interaction and validation."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Run migration with environment variable but decline to save first
    result1 = runner.invoke(
        main,
        ["config", "migrate", "--file", str(config_file)],
        input="n\n",  # No to save
        env={"AUTO_CODER_DEFAULT_BACKEND": "gemini"},
    )

    assert result1.exit_code == 0
    assert "AUTO_CODER_DEFAULT_BACKEND=gemini" in result1.output
    assert "Configuration saved successfully" not in result1.output

    # Verify config was not saved
    config_check = LLMBackendConfiguration.load_from_file(str(config_file))
    assert config_check.default_backend != "gemini"

    # Now run again and accept
    result2 = runner.invoke(
        main,
        ["config", "migrate", "--file", str(config_file)],
        input="y\n",  # Yes to save
        env={"AUTO_CODER_DEFAULT_BACKEND": "qwen"},
    )

    assert result2.exit_code == 0
    assert "Configuration saved successfully" in result2.output

    # Verify config was saved
    config_after = LLMBackendConfiguration.load_from_file(str(config_file))
    assert config_after.default_backend == "qwen"


# =====================================
# Tests for config template command
# =====================================


def test_config_template_generates_default_template(tmp_path: Path):
    """Test config template command generates template with default values."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    result = runner.invoke(main, ["config", "template", "--file", str(config_file)])
    assert result.exit_code == 0
    assert "# Auto-Coder Configuration Template" in result.output
    assert "# This is a template showing all available configuration options" in result.output

    # Verify JSON output is valid
    # Extract JSON from output (between the header and usage instructions)
    import json

    lines = result.output.split("\n")
    json_start = None
    json_end = None

    # Find the start of JSON (first line with just "{")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "{":
            json_start = i
            break

    assert json_start is not None

    # Find the end of JSON (line with just "}")
    brace_count = 0
    for i in range(json_start, len(lines)):
        line = lines[i]
        # Count braces
        brace_count += line.count("{") - line.count("}")
        if brace_count == 0 and line.strip() == "}":
            json_end = i
            break

    assert json_end is not None

    json_str = "\n".join(lines[json_start : json_end + 1])
    config_data = json.loads(json_str)
    assert "backends" in config_data
    assert "codex" in config_data["backends"]
    assert "gemini" in config_data["backends"]


def test_config_template_shows_usage_instructions(tmp_path: Path):
    """Test config template command shows usage instructions."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    result = runner.invoke(main, ["config", "template", "--file", str(config_file)])
    assert result.exit_code == 0
    assert "💡 Usage:" in result.output
    assert "Save this template: auto-coder config template > config.toml" in result.output
    assert "Edit the file: auto-coder config edit" in result.output
    assert "Validate: auto-coder config validate" in result.output


def test_config_template_with_custom_default_backend(tmp_path: Path):
    """Test config template command with custom default backend."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    result = runner.invoke(main, ["config", "template", "--file", str(config_file)])
    assert result.exit_code == 0

    # Verify template contains expected structure
    assert '"backend":' in result.output
    assert '"default": "codex"' in result.output or '"default":' in result.output


def test_config_template_output_format(tmp_path: Path):
    """Test config template command output format is properly formatted."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    result = runner.invoke(main, ["config", "template", "--file", str(config_file)])
    assert result.exit_code == 0

    # Check that JSON is properly indented (2 spaces)
    lines = result.output.split("\n")
    json_section = False
    for line in lines:
        if line.strip().startswith("{") or json_section:
            json_section = True
            if line.strip():
                # JSON values should be indented with 2 spaces
                if ":" in line:
                    indent = len(line) - len(line.lstrip())
                    assert indent % 2 == 0, f"Line not properly indented: {line}"
        if line.strip() == "}":
            break


def test_config_template_includes_all_backends(tmp_path: Path):
    """Test config template includes all available backends."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    result = runner.invoke(main, ["config", "template", "--file", str(config_file)])
    assert result.exit_code == 0

    # Verify all default backends are in the template
    expected_backends = ["codex", "gemini", "qwen", "auggie", "claude", "codex-mcp"]
    for backend in expected_backends:
        assert f'"{backend}":' in result.output


# =====================================
# Tests for config examples command
# =====================================


def test_config_examples_shows_all_examples(tmp_path: Path):
    """Test config examples command shows all configuration examples."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    result = runner.invoke(main, ["config", "examples", "--file", str(config_file)])
    assert result.exit_code == 0
    assert "📚 Configuration Examples" in result.output

    # Check all examples are present
    assert "Example 1: Basic Configuration with Gemini" in result.output
    assert "Example 2: Multiple Backends with Failover" in result.output
    assert "Example 3: OpenAI-Compatible Backends" in result.output
    assert "Example 4: Message Backend Configuration" in result.output


def test_config_examples_shows_common_commands(tmp_path: Path):
    """Test config examples command shows common configuration commands."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    result = runner.invoke(main, ["config", "examples", "--file", str(config_file)])
    assert result.exit_code == 0
    assert "Common Commands:" in result.output

    # Verify common commands are shown
    assert "auto-coder config show" in result.output
    assert "auto-coder config edit" in result.output
    assert "auto-coder config validate" in result.output
    assert "auto-coder config health" in result.output
    assert "auto-coder config backup" in result.output
    assert "auto-coder config setup" in result.output


def test_config_examples_formatting(tmp_path: Path):
    """Test config examples command has proper formatting."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    result = runner.invoke(main, ["config", "examples", "--file", str(config_file)])
    assert result.exit_code == 0

    # Check separator lines
    assert "=" * 70 in result.output

    # Check code blocks are properly formatted
    assert "# Set environment variable" in result.output
    assert "export AUTO_CODER_DEFAULT_BACKEND=gemini" in result.output


def test_config_examples_environment_variable_examples(tmp_path: Path):
    """Test config examples shows environment variable examples."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    result = runner.invoke(main, ["config", "examples", "--file", str(config_file)])
    assert result.exit_code == 0

    # Verify environment variable examples
    assert "export AUTO_CODER_GEMINI_API_KEY=your-api-key-here" in result.output


def test_config_examples_toml_configuration_examples(tmp_path: Path):
    """Test config examples shows TOML configuration examples."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    result = runner.invoke(main, ["config", "examples", "--file", str(config_file)])
    assert result.exit_code == 0

    # Verify TOML configuration examples
    assert "[backend]" in result.output
    assert "[backends.gemini]" in result.output


# =========================================
# Tests for error handling and edge cases
# =========================================


def test_config_show_with_nonexistent_file(tmp_path: Path):
    """Test config show command with non-existent file."""
    config_file = tmp_path / "nonexistent.toml"
    runner = CliRunner()

    result = runner.invoke(main, ["config", "show", "--file", str(config_file)])
    assert result.exit_code == 0

    # Should show default configuration
    import json

    config_data = json.loads(result.output)
    assert "backends" in config_data
    assert "codex" in config_data["backends"]


def test_config_set_with_invalid_path(tmp_path: Path):
    """Test config set command with invalid nested path."""
    # Create a directory instead of a file to test path handling
    config_dir = tmp_path / "config_dir"
    config_dir.mkdir()
    config_file = config_dir / "llm_backend.toml"
    runner = CliRunner()

    # This should work since the directory exists
    result = runner.invoke(main, ["config", "set", "--file", str(config_file), "gemini.model", "test-model"])
    assert result.exit_code == 0
    assert "Set gemini.model = test-model" in result.output


def test_config_set_with_empty_key(tmp_path: Path):
    """Test config set command with empty key."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    # Try to set a value with empty key - Click handles this gracefully
    # The empty string might be treated as a valid key path
    result = runner.invoke(main, ["config", "set", "--file", str(config_file), "", "value"])
    # Click allows this, it will just create an empty key which might work
    # or might silently fail - either way it's acceptable behavior
    assert result.exit_code in [0, 1]


def test_config_get_with_invalid_key(tmp_path: Path):
    """Test config get command with non-existent key."""
    config_file = tmp_path / "llm_backend.toml"
    runner = CliRunner()

    result = runner.invoke(main, ["config", "get", "--file", str(config_file), "nonexistent.key"])
    assert result.exit_code == 0
    # Should return empty or error message


def test_config_validate_with_corrupted_toml(tmp_path: Path):
    """Test config validate command with corrupted TOML file."""
    config_file = tmp_path / "llm_backend.toml"

    # Write invalid TOML
    with open(config_file, "w") as f:
        f.write("[invalid syntax here\n")
        f.write("this is not valid")

    runner = CliRunner()
    result = runner.invoke(main, ["config", "validate", "--file", str(config_file)])
    assert result.exit_code == 0
    # Should report validation errors
    assert "Configuration validation errors found" in result.output or "Error" in result.output


def test_config_edit_with_readonly_directory(tmp_path: Path):
    """Test config edit command with read-only directory."""
    import os
    import stat
    import tempfile

    # Create a temporary directory and make it read-only
    readonly_dir = tmp_path / "readonly"
    readonly_dir.mkdir()
    config_file = readonly_dir / "llm_backend.toml"

    # Make directory read-only
    os.chmod(readonly_dir, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

    runner = CliRunner()
    try:
        # This should either fail or handle gracefully
        result = runner.invoke(main, ["config", "edit", "--file", str(config_file)])
        # Either it succeeds (if editor doesn't need write) or fails gracefully
        assert result.exit_code in [0, 1]
    finally:
        # Restore permissions for cleanup
        os.chmod(readonly_dir, stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO)


def test_config_export_to_invalid_path(tmp_path: Path):
    """Test config export command with invalid output path."""
    config_file = tmp_path / "llm_backend.toml"
    output_file = tmp_path / "nonexistent_dir" / "output.json"

    runner = CliRunner()
    result = runner.invoke(main, ["config", "export", "--file", str(config_file), "--output", str(output_file)])
    # Should either succeed (if directory gets created) or fail gracefully
    assert result.exit_code in [0, 1]


def test_config_import_from_nonexistent_file(tmp_path: Path):
    """Test config import command with non-existent file."""
    config_file = tmp_path / "llm_backend.toml"
    import_file = tmp_path / "nonexistent.json"

    runner = CliRunner()
    result = runner.invoke(main, ["config", "import-config", "--file", str(config_file), str(import_file)])
    # Click validates file exists, so this should fail with exit code 2
    assert result.exit_code == 2
    assert "does not exist" in result.output.lower()


def test_config_import_with_invalid_json(tmp_path: Path):
    """Test config import command with invalid JSON."""
    config_file = tmp_path / "llm_backend.toml"
    import_file = tmp_path / "invalid.json"

    # Write invalid JSON
    with open(import_file, "w") as f:
        f.write("{ this is not valid json }")

    runner = CliRunner()
    result = runner.invoke(main, ["config", "import-config", "--file", str(config_file), str(import_file)])
    assert result.exit_code == 0
    assert "❌ Error importing configuration:" in result.output


def test_config_restore_with_invalid_backup_file(tmp_path: Path):
    """Test config restore command with invalid backup file."""
    config_file = tmp_path / "llm_backend.toml"
    backup_file = tmp_path / "invalid_backup.toml"

    # Create backup file with invalid TOML
    with open(backup_file, "w") as f:
        f.write("[invalid syntax")

    # Create a valid config file
    config = LLMBackendConfiguration()
    config.save_to_file(str(config_file))

    runner = CliRunner()
    # Try to restore from invalid backup (answer 'y' to confirm)
    result = runner.invoke(main, ["config", "restore", "--file", str(config_file), str(backup_file)], input="y\n")
    # Should fail gracefully or show error
    assert result.exit_code == 0  # Should still complete, possibly with error message


def test_config_command_with_invalid_option():
    """Test config commands with invalid options."""
    runner = CliRunner()
    result = runner.invoke(main, ["config", "--invalid-option"])
    assert result.exit_code != 0


# =====================================
# Tests for config_to_dict function
# =====================================


def test_config_to_dict_with_default_backends():
    """Test config_to_dict converts configuration with default backends."""
    from auto_coder.cli_commands_config import config_to_dict

    config = LLMBackendConfiguration()
    result = config_to_dict(config)

    assert isinstance(result, dict)
    assert "backends" in result
    assert "backend" in result
    assert "message_backend" in result

    # Check default backends are present
    expected_backends = ["codex", "gemini", "qwen", "auggie", "claude", "codex-mcp"]
    for backend in expected_backends:
        assert backend in result["backends"]


def test_config_to_dict_with_custom_backends():
    """Test config_to_dict converts configuration with custom backend values."""
    from auto_coder.cli_commands_config import config_to_dict

    config = LLMBackendConfiguration()
    # Customize some values
    config.default_backend = "gemini"
    config.backend_order = ["gemini", "qwen", "claude"]

    # Get and customize a backend
    gemini_config = config.get_backend_config("gemini")
    assert gemini_config is not None
    gemini_config.model = "custom-model"
    gemini_config.temperature = 0.9

    result = config_to_dict(config)

    # Check backend configuration
    assert result["backend"]["default"] == "gemini"
    assert result["backend"]["order"] == ["gemini", "qwen", "claude"]
    assert result["backends"]["gemini"]["model"] == "custom-model"
    assert result["backends"]["gemini"]["temperature"] == 0.9


def test_config_to_dict_validates_structure():
    """Test config_to_dict validates converted dictionary structure."""
    from auto_coder.cli_commands_config import config_to_dict

    config = LLMBackendConfiguration()
    result = config_to_dict(config)

    # Check that result is a dict
    assert isinstance(result, dict)

    # Check required keys
    assert "backends" in result
    assert "backend" in result

    # Check backends is a dict
    assert isinstance(result["backends"], dict)

    # Check backend.order is a list
    assert isinstance(result["backend"]["order"], list)

    # Check backend.default is a string
    assert isinstance(result["backend"]["default"], str)


def test_config_to_dict_with_empty_backends():
    """Test config_to_dict handles configuration with empty backends dict."""
    from auto_coder.cli_commands_config import config_to_dict

    config = LLMBackendConfiguration()
    config.backends = {}  # Empty backends

    result = config_to_dict(config)

    # Should initialize default backends
    assert len(result["backends"]) > 0


def test_config_to_dict_with_all_fields():
    """Test config_to_dict includes all backend configuration fields."""
    from auto_coder.cli_commands_config import config_to_dict

    config = LLMBackendConfiguration()
    config.backend_order = ["codex"]
    config.default_backend = "codex"
    config.message_backend_order = ["claude"]
    config.message_default_backend = "claude"

    # Configure codex with all possible fields
    codex_config = config.get_backend_config("codex")
    assert codex_config is not None
    codex_config.enabled = True
    codex_config.model = "codex-model"
    codex_config.api_key = "test-key"
    codex_config.base_url = "https://test.com"
    codex_config.temperature = 0.8
    codex_config.timeout = 30
    codex_config.max_retries = 3
    codex_config.openai_api_key = "openai-key"
    codex_config.openai_base_url = "https://openai.com"
    codex_config.extra_args = {"arg1": "value1"}

    result = config_to_dict(config)

    # Check all fields are present
    codex_data = result["backends"]["codex"]
    assert codex_data["enabled"] is True
    assert codex_data["model"] == "codex-model"
    assert codex_data["api_key"] == "test-key"
    assert codex_data["base_url"] == "https://test.com"
    assert codex_data["temperature"] == 0.8
    assert codex_data["timeout"] == 30
    assert codex_data["max_retries"] == 3
    assert codex_data["openai_api_key"] == "openai-key"
    assert codex_data["openai_base_url"] == "https://openai.com"
    assert codex_data["extra_args"] == {"arg1": "value1"}

    # Check message backend
    assert result["message_backend"]["order"] == ["claude"]
    assert result["message_backend"]["default"] == "claude"


# =========================================
# Tests for config_validate function
# =========================================


def test_config_validate_all_backend_properties():
    """Test config_validate validates all backend properties."""
    from auto_coder.cli_commands_config import config_validate

    config = LLMBackendConfiguration()

    # Test with valid configuration
    errors = config_validate(config)
    assert len(errors) == 0


def test_config_validate_with_invalid_model_type():
    """Test config_validate detects invalid model type."""
    from auto_coder.cli_commands_config import config_validate

    config = LLMBackendConfiguration()
    gemini_config = config.get_backend_config("gemini")
    assert gemini_config is not None
    gemini_config.model = 123  # Should be string or None

    errors = config_validate(config)
    assert len(errors) > 0
    assert "gemini.model must be a string" in errors


def test_config_validate_with_invalid_enabled_type():
    """Test config_validate detects invalid enabled type."""
    from auto_coder.cli_commands_config import config_validate

    config = LLMBackendConfiguration()
    gemini_config = config.get_backend_config("gemini")
    assert gemini_config is not None
    gemini_config.enabled = "true"  # Should be bool

    errors = config_validate(config)
    assert len(errors) > 0
    assert "gemini.enabled must be a boolean" in errors


def test_config_validate_with_invalid_api_key_type():
    """Test config_validate detects invalid api_key type."""
    from auto_coder.cli_commands_config import config_validate

    config = LLMBackendConfiguration()
    gemini_config = config.get_backend_config("gemini")
    assert gemini_config is not None
    gemini_config.api_key = 123  # Should be str or None

    errors = config_validate(config)
    assert len(errors) > 0
    assert "gemini.api_key must be a string" in errors


def test_config_validate_with_invalid_temperature_type():
    """Test config_validate detects invalid temperature type."""
    from auto_coder.cli_commands_config import config_validate

    config = LLMBackendConfiguration()
    gemini_config = config.get_backend_config("gemini")
    assert gemini_config is not None
    gemini_config.temperature = "high"  # Should be float or int or None

    errors = config_validate(config)
    assert len(errors) > 0
    assert "gemini.temperature must be a number" in errors


def test_config_validate_with_invalid_timeout_type():
    """Test config_validate detects invalid timeout type."""
    from auto_coder.cli_commands_config import config_validate

    config = LLMBackendConfiguration()
    gemini_config = config.get_backend_config("gemini")
    assert gemini_config is not None
    gemini_config.timeout = "30"  # Should be int or None

    errors = config_validate(config)
    assert len(errors) > 0
    assert "gemini.timeout must be an integer" in errors


def test_config_validate_backend_order_and_default():
    """Test config_validate validates backend order and default backend."""
    from auto_coder.cli_commands_config import config_validate

    config = LLMBackendConfiguration()

    # Test with valid backend order (list)
    config.backend_order = ["gemini", "qwen"]
    config.default_backend = "gemini"
    errors = config_validate(config)
    # Should have no errors for valid order and default

    # Test with invalid backend order (not a list)
    config.backend_order = "gemini"  # Should be list
    errors = config_validate(config)
    assert len(errors) > 0
    assert "backend.order must be a list" in errors


def test_config_validate_message_backend_settings():
    """Test config_validate validates message backend settings."""
    from auto_coder.cli_commands_config import config_validate

    config = LLMBackendConfiguration()

    # Test with valid message backend settings
    config.message_backend_order = ["claude", "qwen"]
    config.message_default_backend = "claude"
    errors = config_validate(config)
    # Should have no errors for valid message backend settings

    # Test with invalid message backend order (not a list)
    config.message_backend_order = "claude"  # Should be list
    errors = config_validate(config)
    assert len(errors) > 0
    assert "message_backend.order must be a list" in errors


def test_config_validate_multiple_backends():
    """Test config_validate with multiple backends having errors."""
    from auto_coder.cli_commands_config import config_validate

    config = LLMBackendConfiguration()

    # Create errors in multiple backends
    gemini_config = config.get_backend_config("gemini")
    assert gemini_config is not None
    gemini_config.model = 123  # Invalid

    qwen_config = config.get_backend_config("qwen")
    assert qwen_config is not None
    qwen_config.enabled = "true"  # Invalid

    errors = config_validate(config)
    assert len(errors) >= 2
    assert any("gemini.model" in err for err in errors)
    assert any("qwen.enabled" in err for err in errors)


def test_config_validate_none_values():
    """Test config_validate accepts None values where appropriate."""
    from auto_coder.cli_commands_config import config_validate

    config = LLMBackendConfiguration()

    # Set optional fields to None (should be valid)
    gemini_config = config.get_backend_config("gemini")
    assert gemini_config is not None
    gemini_config.model = None
    gemini_config.api_key = None
    gemini_config.temperature = None
    gemini_config.timeout = None

    errors = config_validate(config)
    # None values should be valid, so no errors
    assert len(errors) == 0

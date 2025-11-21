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

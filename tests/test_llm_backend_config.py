"""
Unit tests for LLM Backend Configuration Management Module.

These tests verify the functionality of the LLM backend configuration system,
including file operations, validation, and configuration management features.
"""

import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import toml

from auto_coder.llm_backend_config import (
    AuggieBackendConfig,
    BackendConfig,
    ClaudeBackendConfig,
    CodexBackendConfig,
    GeminiBackendConfig,
    LLMBackendConfig,
    LLMBackendConfigManager,
    QwenBackendConfig,
    ensure_config_directory,
    get_llm_backend_config,
    initialize_llm_backend_config,
)


class TestLLMBackendConfig:
    """Test cases for LLMBackendConfig class."""

    def test_default_config_creation(self):
        """Test that default configuration is created correctly."""
        config = LLMBackendConfig()

        # Verify default values
        assert config.version == LLMBackendConfig.CONFIG_VERSION
        assert isinstance(config.created_at, str)
        assert isinstance(config.updated_at, str)

        # Verify backend configurations exist with default values
        assert config.codex is not None
        assert config.gemini is not None
        assert config.qwen is not None
        assert config.claude is not None
        assert config.auggie is not None

        # Verify default backend configs have expected attributes
        assert hasattr(config.codex, "api_key")
        assert hasattr(config.codex, "model")
        assert hasattr(config.gemini, "api_key")
        assert hasattr(config.gemini, "model")

    def test_to_dict_conversion(self):
        """Test conversion of configuration to dictionary."""
        config = LLMBackendConfig()
        config_dict = config.to_dict()

        # Verify required top-level keys exist
        assert "version" in config_dict
        assert "created_at" in config_dict
        assert "updated_at" in config_dict
        assert "backends" in config_dict

        # Verify backend configurations exist in dict
        backends = config_dict["backends"]
        assert "codex" in backends
        assert "gemini" in backends
        assert "qwen" in backends
        assert "claude" in backends
        assert "auggie" in backends

    def test_from_dict_conversion(self):
        """Test creation of configuration from dictionary."""
        sample_data = {
            "version": "1.0.0",
            "created_at": "2023-01-01T00:00:00",
            "updated_at": "2023-01-01T00:00:00",
            "backends": {
                "codex": {
                    "api_key": "test_codex_key",
                    "model": "gpt-4",
                    "temperature": 0.7,
                },
                "gemini": {
                    "api_key": "test_gemini_key",
                    "model": "gemini-pro",
                    "temperature": 0.5,
                },
            },
        }

        config = LLMBackendConfig.from_dict(sample_data)

        # Verify values were set correctly
        assert config.version == "1.0.0"
        assert config.codex.api_key == "test_codex_key"
        assert config.codex.model == "gpt-4"
        assert config.codex.temperature == 0.7
        assert config.gemini.api_key == "test_gemini_key"
        assert config.gemini.model == "gemini-pro"
        assert config.gemini.temperature == 0.5

    def test_save_to_file(self):
        """Test saving configuration to file."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "test_config.toml"

            config = LLMBackendConfig()
            config.codex.api_key = "test_key"
            config.gemini.model = "gemini-test"

            # Save the configuration
            config.save_to_file(config_path)

            # Verify file was created
            assert config_path.exists()

            # Load the file and verify content
            loaded_data = toml.load(config_path)
            assert loaded_data["backends"]["codex"]["api_key"] == "test_key"
            assert loaded_data["backends"]["gemini"]["model"] == "gemini-test"

    def test_load_from_file(self):
        """Test loading configuration from file."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "test_config.toml"

            # Create a sample config file
            sample_config = {
                "version": "1.0.0",
                "created_at": "2023-01-01T00:00:00",
                "updated_at": "2023-01-01T00:00:00",
                "backends": {
                    "codex": {
                        "api_key": "loaded_key",
                        "model": "gpt-test",
                    },
                    "gemini": {
                        "api_key": "loaded_gemini_key",
                        "model": "gemini-test",
                    },
                },
            }

            with open(config_path, "w") as f:
                toml.dump(sample_config, f)

            # Load the configuration
            config = LLMBackendConfig.load_from_file(config_path)

            # Verify values were loaded correctly
            assert config.codex.api_key == "loaded_key"
            assert config.codex.model == "gpt-test"
            assert config.gemini.api_key == "loaded_gemini_key"
            assert config.gemini.model == "gemini-test"

    def test_load_from_file_nonexistent(self):
        """Test loading configuration from non-existent file returns default."""
        config = LLMBackendConfig.load_from_file(Path("/nonexistent/path.toml"))

        # Should return default config without error
        assert config.version == LLMBackendConfig.CONFIG_VERSION
        assert config.codex is not None

    def test_generate_default_config(self):
        """Test generation of default configuration."""
        config = LLMBackendConfig()
        default_config = config.generate_default_config()

        # Verify required sections exist
        assert "version" in default_config
        assert "backends" in default_config
        assert "defaults" in default_config

        # Verify all backends have entries
        backends = default_config["backends"]
        assert "codex" in backends
        assert "codex_mcp" in backends
        assert "gemini" in backends
        assert "qwen" in backends
        assert "claude" in backends
        assert "auggie" in backends

    def test_save_default_config(self):
        """Test saving default configuration."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "default_config.toml"

            config = LLMBackendConfig()
            config.save_default_config(config_path)

            # Verify file was created
            assert config_path.exists()

            # Load and verify it's valid TOML
            loaded_data = toml.load(config_path)
            assert "backends" in loaded_data
            assert "codex" in loaded_data["backends"]

    def test_validate_config_valid(self):
        """Test validation of valid configuration."""
        config = LLMBackendConfig()
        config.gemini.api_key = "valid_key_12345"  # Reasonable length
        config.gemini.model = "gemini-pro"

        errors = config.validate_config()
        assert len(errors) == 0

    def test_validate_config_invalid_api_key(self):
        """Test validation of configuration with invalid API key."""
        config = LLMBackendConfig()
        config.gemini.api_key = "short"  # Too short

        errors = config.validate_config()
        assert len(errors) > 0
        assert "too short" in errors[0]

    def test_validate_config_invalid_model(self):
        """Test validation of configuration with invalid model name."""
        config = LLMBackendConfig()
        config.gemini.api_key = "valid_key_12345"
        config.gemini.model = "model with spaces"  # Invalid characters

        errors = config.validate_config()
        assert len(errors) > 0
        assert "contains invalid characters" in errors[0]

    def test_validate_and_save(self):
        """Test validation and save functionality."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "validated_config.toml"

            config = LLMBackendConfig()
            config.gemini.api_key = "valid_key_12345"
            config.gemini.model = "gemini-pro"

            # Should save successfully with valid config
            result = config.validate_and_save(config_path)
            assert result is True
            assert config_path.exists()

    def test_validate_and_save_invalid(self):
        """Test validation and save with invalid config."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "invalid_config.toml"

            config = LLMBackendConfig()
            config.gemini.api_key = "short"  # Invalid

            # Should not save with invalid config
            result = config.validate_and_save(config_path)
            assert result is False
            assert not config_path.exists()

    def test_has_changes_detection(self):
        """Test configuration change detection."""
        config = LLMBackendConfig()
        original_hash = config._original_config_hash

        # Initially no changes
        assert config.has_changes() is False

        # Modify a value
        config.gemini.api_key = "new_key"

        # Should detect changes
        assert config.has_changes() is True

    def test_create_backup(self):
        """Test configuration backup functionality."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"

            # Create initial config file
            config = LLMBackendConfig()
            config.gemini.api_key = "backup_test_key"
            config.save_to_file(config_path)

            # Create backup
            backup_path = config.create_backup()

            # Verify backup exists and has same content
            assert backup_path.exists()
            assert backup_path != config_path

            original_data = toml.load(config_path)
            backup_data = toml.load(backup_path)
            assert original_data == backup_data

    def test_import_config(self):
        """Test importing configuration from another file."""
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "source_config.toml"
            target_path = Path(temp_dir) / "target_config.toml"

            # Create source config
            source_config = LLMBackendConfig()
            source_config.gemini.api_key = "import_source_key"
            source_config.gemini.model = "import-source-model"
            source_config.save_to_file(source_path)

            # Create target config with different values
            target_config = LLMBackendConfig()
            target_config.gemini.api_key = "original_key"
            target_config.gemini.model = "original-model"

            # Import from source
            success = target_config.import_config(source_path)

            assert success is True
            assert target_config.gemini.api_key == "import_source_key"
            assert target_config.gemini.model == "import-source-model"

    def test_export_config(self):
        """Test exporting configuration to another file."""
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "source_config.toml"
            export_path = Path(temp_dir) / "export_config.toml"

            # Create source config
            config = LLMBackendConfig()
            config.gemini.api_key = "export_source_key"
            config.gemini.model = "export-source-model"
            config.save_to_file(source_path)

            # Export to different file
            success = config.export_config(export_path)

            assert success is True
            assert export_path.exists()

            # Verify exported content matches
            exported_config = LLMBackendConfig.load_from_file(export_path)
            assert exported_config.gemini.api_key == "export_source_key"
            assert exported_config.gemini.model == "export-source-model"

    def test_get_backend_config(self):
        """Test getting configuration for a specific backend."""
        config = LLMBackendConfig()
        config.gemini.api_key = "specific_backend_key"

        gemini_config = config.get_backend_config("gemini")
        assert gemini_config is not None
        assert gemini_config.api_key == "specific_backend_key"

        # Test non-existent backend
        nonexistent_config = config.get_backend_config("nonexistent")
        assert nonexistent_config is None

    def test_set_backend_config(self):
        """Test setting configuration for a specific backend."""
        config = LLMBackendConfig()

        # Create new backend config
        new_gemini_config = GeminiBackendConfig(api_key="new_gemini_key", model="new-gemini-model")

        # Set it
        success = config.set_backend_config("gemini", new_gemini_config)
        assert success is True
        assert config.gemini.api_key == "new_gemini_key"
        assert config.gemini.model == "new-gemini-model"

        # Test setting non-existent backend
        success = config.set_backend_config("nonexistent", new_gemini_config)
        assert success is False

    def test_apply_environment_overrides(self):
        """Test applying environment variable overrides."""
        config = LLMBackendConfig()

        # Set up environment variables
        with patch.dict(os.environ, {"GEMINI_API_KEY": "env_gemini_key", "GEMINI_MODEL": "env-gemini-model", "QWEN_API_KEY": "env_qwen_key"}):
            config.apply_environment_overrides()

            # Verify environment variables were applied
            assert config.gemini.api_key == "env_gemini_key"
            assert config.gemini.model == "env-gemini-model"
            assert config.qwen.api_key == "env_qwen_key"

    def test_get_diff(self):
        """Test configuration difference functionality."""
        # Create base config
        base_config = LLMBackendConfig()
        base_config.gemini.api_key = "base_key"
        base_config.gemini.model = "base-model"
        base_config.qwen.api_key = "base_qwen_key"

        # Create modified config
        modified_config = LLMBackendConfig()
        modified_config.gemini.api_key = "modified_key"  # Changed
        modified_config.gemini.model = "base-model"  # Same
        modified_config.qwen.api_key = "different_qwen_key"  # Changed

        # Get diff
        diff = base_config.get_diff(modified_config)

        # Verify differences are detected
        assert "backends" in diff
        # Should show differences in api_key values
        assert diff["backends"]["old"]["gemini"]["api_key"] == "base_key"
        assert diff["backends"]["new"]["gemini"]["api_key"] == "modified_key"


class TestLLMBackendConfigManager:
    """Test cases for LLMBackendConfigManager singleton."""

    def test_singleton_pattern(self):
        """Test that the manager follows singleton pattern."""
        manager1 = LLMBackendConfigManager()
        manager2 = LLMBackendConfigManager()

        assert manager1 is manager2

    def test_initialize_and_get_config(self):
        """Test initializing and retrieving configuration."""
        manager = LLMBackendConfigManager()

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "manager_test.toml"

            # Initialize with specific path
            manager.initialize(config_path)

            # Get config and verify it's loaded
            config = manager.get_config()
            assert config is not None
            assert isinstance(config, LLMBackendConfig)

    def test_has_config_changes(self):
        """Test checking for configuration changes."""
        manager = LLMBackendConfigManager()

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "changes_test.toml"

            manager.initialize(config_path)
            config = manager.get_config()

            # Initially no changes
            assert manager.has_config_changes() is False

            # Make a change
            config.gemini.api_key = "changed_key"

            # Should detect changes
            assert manager.has_config_changes() is True

    def test_save_config(self):
        """Test saving configuration via manager."""
        manager = LLMBackendConfigManager()

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "save_test.toml"

            manager.initialize(config_path)
            config = manager.get_config()

            # Make a change and save
            config.gemini.api_key = "saved_key"
            result = manager.save_config()

            assert result is True

            # Verify file was saved with new value
            loaded_config = LLMBackendConfig.load_from_file(config_path)
            assert loaded_config.gemini.api_key == "saved_key"

    def test_reload_config(self):
        """Test reloading configuration from file."""
        manager = LLMBackendConfigManager()

        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "reload_test.toml"

            # Create initial config
            initial_config = LLMBackendConfig()
            initial_config.gemini.api_key = "initial_key"
            initial_config.save_to_file(config_path)

            # Initialize manager and modify in memory
            manager.initialize(config_path)
            config = manager.get_config()
            config.gemini.api_key = "modified_in_memory"

            # Create different config on disk
            disk_config = LLMBackendConfig()
            disk_config.gemini.api_key = "changed_on_disk"
            disk_config.save_to_file(config_path)

            # Reload and verify memory config matches disk
            manager.reload_config()
            assert config.gemini.api_key == "changed_on_disk"


class TestGlobalFunctions:
    """Test global functions for configuration access."""

    def test_get_llm_backend_config(self):
        """Test getting the global configuration."""
        # Reset the singleton instance
        LLMBackendConfigManager._instance = None

        config = get_llm_backend_config()
        assert config is not None
        assert isinstance(config, LLMBackendConfig)

    def test_initialize_llm_backend_config(self):
        """Test initializing the global configuration."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "init_test.toml"

            # Initialize with specific path
            initialize_llm_backend_config(config_path)

            # Get config and verify it's using the right path
            config = get_llm_backend_config()
            assert config is not None

    def test_ensure_config_directory(self):
        """Test ensuring config directory exists."""
        # Test with a temporary directory
        with tempfile.TemporaryDirectory() as temp_dir:
            # Temporarily change the default directory
            original_default = LLMBackendConfig.DEFAULT_CONFIG_DIR
            LLMBackendConfig.DEFAULT_CONFIG_DIR = Path(temp_dir) / ".auto-coder"

            try:
                ensure_config_directory()
                # Verify directory was created
                assert LLMBackendConfig.DEFAULT_CONFIG_DIR.exists()
                assert LLMBackendConfig.DEFAULT_CONFIG_DIR.is_dir()
            finally:
                # Restore original value
                LLMBackendConfig.DEFAULT_CONFIG_DIR = original_default

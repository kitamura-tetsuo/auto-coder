"""Tests for backend_provider_manager module."""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import toml

from src.auto_coder.backend_provider_manager import (
    BackendProviderManager,
    BackendProviderMetadata,
    ProviderMetadata,
)


class TestProviderMetadata:
    """Test cases for ProviderMetadata class."""

    def test_provider_metadata_creation(self):
        """Test creating a ProviderMetadata with default values."""
        provider = ProviderMetadata(name="test-provider", command="uvx")
        assert provider.name == "test-provider"
        assert provider.command == "uvx"
        assert provider.args == []
        assert provider.description is None
        assert provider.uppercase_settings == {}

    def test_provider_metadata_with_custom_values(self):
        """Test creating a ProviderMetadata with custom values."""
        provider = ProviderMetadata(
            name="qwen-open-router",
            command="uvx",
            args=["qwen-openai-proxy"],
            description="Qwen via OpenRouter API",
            uppercase_settings={"OPENROUTER_API_KEY": "key123", "AZURE_ENDPOINT": "https://example.com"},
        )
        assert provider.name == "qwen-open-router"
        assert provider.command == "uvx"
        assert provider.args == ["qwen-openai-proxy"]
        assert provider.description == "Qwen via OpenRouter API"
        assert provider.uppercase_settings["OPENROUTER_API_KEY"] == "key123"
        assert provider.uppercase_settings["AZURE_ENDPOINT"] == "https://example.com"


class TestBackendProviderMetadata:
    """Test cases for BackendProviderMetadata class."""

    def test_backend_provider_metadata_creation(self):
        """Test creating a BackendProviderMetadata with default values."""
        backend_metadata = BackendProviderMetadata(backend_name="qwen")
        assert backend_metadata.backend_name == "qwen"
        assert backend_metadata.providers == []

    def test_backend_provider_metadata_with_providers(self):
        """Test creating a BackendProviderMetadata with providers."""
        provider1 = ProviderMetadata(name="provider1", command="cmd1")
        provider2 = ProviderMetadata(name="provider2", command="cmd2")
        backend_metadata = BackendProviderMetadata(backend_name="qwen", providers=[provider1, provider2])

        assert backend_metadata.backend_name == "qwen"
        assert len(backend_metadata.providers) == 2
        assert backend_metadata.providers[0].name == "provider1"
        assert backend_metadata.providers[1].name == "provider2"

    def test_get_provider(self):
        """Test getting a specific provider by name."""
        provider1 = ProviderMetadata(name="provider1", command="cmd1")
        provider2 = ProviderMetadata(name="provider2", command="cmd2")
        backend_metadata = BackendProviderMetadata(backend_name="qwen", providers=[provider1, provider2])

        found = backend_metadata.get_provider("provider1")
        assert found is not None
        assert found.name == "provider1"

        not_found = backend_metadata.get_provider("nonexistent")
        assert not_found is None

    def test_get_provider_names(self):
        """Test getting list of provider names."""
        provider1 = ProviderMetadata(name="provider1", command="cmd1")
        provider2 = ProviderMetadata(name="provider2", command="cmd2")
        backend_metadata = BackendProviderMetadata(backend_name="qwen", providers=[provider1, provider2])

        names = backend_metadata.get_provider_names()
        assert names == ["provider1", "provider2"]


class TestBackendProviderManager:
    """Test cases for BackendProviderManager class."""

    def test_default_initialization(self):
        """Test BackendProviderManager with default initialization."""
        manager = BackendProviderManager()
        assert manager.metadata_path.endswith(".auto-coder/provider_metadata.toml")

    def test_custom_path_initialization(self):
        """Test BackendProviderManager with custom metadata path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_path = Path(tmpdir) / "custom_metadata.toml"
            manager = BackendProviderManager(str(custom_path))
            assert manager.metadata_path == str(custom_path)

    def test_load_provider_metadata_nonexistent_file(self):
        """Test loading provider metadata from nonexistent file (graceful degradation)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            metadata_file = Path(tmpdir) / "nonexistent.toml"
            manager = BackendProviderManager(str(metadata_file))
            manager.load_provider_metadata()

            # Should degrade gracefully with empty cache
            backend_metadata = manager.get_backend_providers("qwen")
            assert backend_metadata.backend_name == "qwen"
            assert backend_metadata.providers == []

    def test_load_provider_metadata_empty_file(self):
        """Test loading provider metadata from empty file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            metadata_file = Path(tmpdir) / "empty_metadata.toml"
            # Create empty file
            metadata_file.touch()

            manager = BackendProviderManager(str(metadata_file))
            manager.load_provider_metadata()

            backend_metadata = manager.get_backend_providers("qwen")
            assert backend_metadata.providers == []

    def test_load_provider_metadata_valid_file(self):
        """Test loading provider metadata from valid file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            metadata_file = Path(tmpdir) / "valid_metadata.toml"

            # Create metadata file
            metadata = {
                "qwen": {
                    "qwen-open-router": {
                        "command": "uvx",
                        "args": ["qwen-openai-proxy"],
                        "description": "Qwen via OpenRouter API",
                        "OPENROUTER_API_KEY": "key123",
                    },
                    "qwen-azure": {
                        "command": "uvx",
                        "args": ["qwen-azure-proxy"],
                        "description": "Qwen via Azure OpenAI",
                        "AZURE_ENDPOINT": "https://example.openai.azure.com",
                        "AZURE_API_VERSION": "2024-02-15-preview",
                    },
                }
            }

            with open(metadata_file, "w") as f:
                toml.dump(metadata, f)

            manager = BackendProviderManager(str(metadata_file))
            manager.load_provider_metadata()

            # Test getting providers for qwen backend
            backend_metadata = manager.get_backend_providers("qwen")
            assert backend_metadata.backend_name == "qwen"
            assert len(backend_metadata.providers) == 2

            # Test first provider
            provider1 = backend_metadata.get_provider("qwen-open-router")
            assert provider1 is not None
            assert provider1.name == "qwen-open-router"
            assert provider1.command == "uvx"
            assert provider1.args == ["qwen-openai-proxy"]
            assert provider1.description == "Qwen via OpenRouter API"
            assert provider1.uppercase_settings["OPENROUTER_API_KEY"] == "key123"

            # Test second provider
            provider2 = backend_metadata.get_provider("qwen-azure")
            assert provider2 is not None
            assert provider2.name == "qwen-azure"
            assert provider2.command == "uvx"
            assert provider2.args == ["qwen-azure-proxy"]
            assert provider2.description == "Qwen via Azure OpenAI"
            assert provider2.uppercase_settings["AZURE_ENDPOINT"] == "https://example.openai.azure.com"
            assert provider2.uppercase_settings["AZURE_API_VERSION"] == "2024-02-15-preview"

    def test_lazy_loading(self):
        """Test that metadata is loaded lazily on first access."""
        with tempfile.TemporaryDirectory() as tmpdir:
            metadata_file = Path(tmpdir) / "test_metadata.toml"

            # Create metadata file
            metadata = {
                "gemini": {
                    "gemini-direct": {
                        "command": "uvx",
                        "description": "Direct Gemini API",
                    }
                }
            }

            with open(metadata_file, "w") as f:
                toml.dump(metadata, f)

            manager = BackendProviderManager(str(metadata_file))

            # Metadata should not be loaded yet
            assert manager._metadata_cache is None

            # Access providers - this should trigger lazy loading
            backend_metadata = manager.get_backend_providers("gemini")

            # Now metadata should be loaded
            assert manager._metadata_cache is not None
            assert len(backend_metadata.providers) == 1

    def test_get_all_provider_names(self):
        """Test getting all provider names for a backend."""
        with tempfile.TemporaryDirectory() as tmpdir:
            metadata_file = Path(tmpdir) / "test_metadata.toml"

            metadata = {
                "claude": {
                    "claude-direct": {
                        "command": "uvx",
                        "description": "Direct Claude API",
                    },
                    "claude-proxy": {
                        "command": "uvx",
                        "description": "Claude via proxy",
                    },
                }
            }

            with open(metadata_file, "w") as f:
                toml.dump(metadata, f)

            manager = BackendProviderManager(str(metadata_file))
            names = manager.get_all_provider_names("claude")

            assert names == ["claude-direct", "claude-proxy"]

    def test_has_providers(self):
        """Test checking if a backend has providers."""
        with tempfile.TemporaryDirectory() as tmpdir:
            metadata_file = Path(tmpdir) / "test_metadata.toml"

            metadata = {
                "qwen": {
                    "qwen-provider": {
                        "command": "uvx",
                        "description": "Test provider",
                    }
                }
            }

            with open(metadata_file, "w") as f:
                toml.dump(metadata, f)

            manager = BackendProviderManager(str(metadata_file))

            # Backend with providers
            assert manager.has_providers("qwen") is True

            # Backend without providers
            assert manager.has_providers("nonexistent") is False

    def test_provider_count_and_env_context(self):
        """Ensure provider counts and env contexts are derived from metadata."""
        manager = BackendProviderManager()
        manager._provider_cache["codex"] = BackendProviderMetadata(
            backend_name="codex",
            providers=[
                ProviderMetadata(name="codex-primary", command="uvx", uppercase_settings={"TOKEN": "alpha"}),
                ProviderMetadata(name="codex-secondary", command="uvx", uppercase_settings={"TOKEN": "beta"}),
            ],
        )

        assert manager.get_provider_count("codex") == 2
        env_vars = manager.create_env_context("codex")
        assert env_vars == {"TOKEN": "alpha"}
        assert manager.get_current_provider_name("codex") == "codex-primary"

    def test_provider_rotation_and_tracking(self):
        """Providers rotate in a circular manner and track last used provider."""
        manager = BackendProviderManager()
        manager._provider_cache["gemini"] = BackendProviderMetadata(
            backend_name="gemini",
            providers=[
                ProviderMetadata(name="gemini-direct", command="uvx"),
                ProviderMetadata(name="gemini-proxy", command="uvx"),
            ],
        )

        assert manager.get_current_provider_name("gemini") == "gemini-direct"
        assert manager.advance_to_next_provider("gemini") is True
        assert manager.get_current_provider_name("gemini") == "gemini-proxy"
        assert manager.advance_to_next_provider("gemini") is True
        assert manager.get_current_provider_name("gemini") == "gemini-direct"

        manager.mark_provider_used("gemini", "gemini-proxy")
        assert manager.get_last_used_provider_name("gemini") == "gemini-proxy"

    def test_clear_cache(self):
        """Test clearing the provider metadata cache."""
        with tempfile.TemporaryDirectory() as tmpdir:
            metadata_file = Path(tmpdir) / "test_metadata.toml"

            metadata = {
                "test": {
                    "provider1": {
                        "command": "uvx",
                        "description": "Test",
                    }
                }
            }

            with open(metadata_file, "w") as f:
                toml.dump(metadata, f)

            manager = BackendProviderManager(str(metadata_file))

            # Load metadata
            backend_metadata = manager.get_backend_providers("test")
            assert len(backend_metadata.providers) == 1
            assert manager._metadata_cache is not None

            # Clear cache
            manager.clear_cache()

            # Cache should be cleared
            assert len(backend_metadata.providers) == 1  # Existing object still exists
            assert manager._metadata_cache is None

            # Access again - should load from file again
            backend_metadata2 = manager.get_backend_providers("test")
            assert len(backend_metadata2.providers) == 1

    def test_get_default_manager(self):
        """Test getting a default manager instance."""
        manager1 = BackendProviderManager.get_default_manager()
        manager2 = BackendProviderManager.get_default_manager()

        # Should return a valid manager
        assert isinstance(manager1, BackendProviderManager)
        assert isinstance(manager2, BackendProviderManager)

    def test_cache_persistence(self):
        """Test that provider metadata is cached correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            metadata_file = Path(tmpdir) / "test_metadata.toml"

            metadata = {
                "cached": {
                    "provider1": {
                        "command": "uvx",
                        "description": "Cached provider",
                    }
                }
            }

            with open(metadata_file, "w") as f:
                toml.dump(metadata, f)

            manager = BackendProviderManager(str(metadata_file))

            # First access - loads and caches
            backend1 = manager.get_backend_providers("cached")
            assert len(backend1.providers) == 1

            # Second access - uses cache
            backend2 = manager.get_backend_providers("cached")
            assert len(backend2.providers) == 1

            # Should be the same cached object
            assert backend1 is backend2

    def test_invalid_toml_file(self):
        """Test handling of invalid TOML file (graceful degradation)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            metadata_file = Path(tmpdir) / "invalid_metadata.toml"

            # Write invalid TOML
            with open(metadata_file, "w") as f:
                f.write("invalid toml syntax [[[")

            manager = BackendProviderManager(str(metadata_file))
            manager.load_provider_metadata()

            # Should degrade gracefully
            backend_metadata = manager.get_backend_providers("qwen")
            assert backend_metadata.providers == []

    def test_multiple_backends(self):
        """Test loading provider metadata for multiple backends."""
        with tempfile.TemporaryDirectory() as tmpdir:
            metadata_file = Path(tmpdir) / "multi_backend_metadata.toml"

            metadata = {
                "qwen": {
                    "qwen-provider": {
                        "command": "uvx",
                        "description": "Qwen provider",
                    }
                },
                "gemini": {
                    "gemini-provider": {
                        "command": "uvx",
                        "description": "Gemini provider",
                    }
                },
                "claude": {
                    "claude-provider": {
                        "command": "uvx",
                        "description": "Claude provider",
                    }
                },
            }

            with open(metadata_file, "w") as f:
                toml.dump(metadata, f)

            manager = BackendProviderManager(str(metadata_file))

            # Test each backend
            qwen_metadata = manager.get_backend_providers("qwen")
            assert len(qwen_metadata.providers) == 1
            assert qwen_metadata.providers[0].name == "qwen-provider"

            gemini_metadata = manager.get_backend_providers("gemini")
            assert len(gemini_metadata.providers) == 1
            assert gemini_metadata.providers[0].name == "gemini-provider"

            claude_metadata = manager.get_backend_providers("claude")
            assert len(claude_metadata.providers) == 1
            assert claude_metadata.providers[0].name == "claude-provider"

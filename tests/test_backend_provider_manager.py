"""Tests for backend_provider_manager module."""

import pytest

from auto_coder.backend_provider_manager import BackendProviderManager
from auto_coder.llm_backend_config import LLMBackendConfiguration, ProviderConfig


class TestProviderConfig:
    """Test cases for ProviderConfig class."""

    def test_provider_config_creation(self):
        """Test creating a ProviderConfig with default values."""
        provider = ProviderConfig(name="test-provider")
        assert provider.name == "test-provider"
        assert provider.command is None
        assert provider.description is None
        assert provider.settings == {}

    def test_provider_config_with_custom_values(self):
        """Test creating a ProviderConfig with custom values."""
        provider = ProviderConfig(
            name="qwen-open-router",
            command="uvx",
            description="Qwen via OpenRouter",
            settings={"API_BASE": "https://openrouter.ai/api/v1", "TIMEOUT": "30"},
        )
        assert provider.name == "qwen-open-router"
        assert provider.command == "uvx"
        assert provider.description == "Qwen via OpenRouter"
        assert provider.settings["API_BASE"] == "https://openrouter.ai/api/v1"
        assert provider.settings["TIMEOUT"] == "30"

    def test_provider_config_settings_default(self):
        """Test that settings has a proper default factory."""
        provider1 = ProviderConfig(name="test1")
        provider2 = ProviderConfig(name="test2")

        # Modifying one shouldn't affect the other
        provider1.settings["TEST"] = "value"
        assert "TEST" not in provider2.settings


class TestBackendProviderManager:
    """Test cases for BackendProviderManager class."""

    def test_manager_initialization(self):
        """Test BackendProviderManager initialization."""
        config = LLMBackendConfiguration()
        manager = BackendProviderManager(config)
        assert manager._config is config
        assert manager._provider_configs == {}

    def test_get_providers_for_backend_with_providers(self):
        """Test getting providers for a backend that has providers configured."""
        config = LLMBackendConfiguration()

        # Add provider configurations
        config.providers["provider1"] = ProviderConfig(name="provider1", command="uvx", description="Provider 1")
        config.providers["provider2"] = ProviderConfig(name="provider2", command="python", description="Provider 2")

        # Configure backend to use providers
        backend_config = config.get_backend_config("qwen")
        backend_config.providers = ["provider1", "provider2"]

        manager = BackendProviderManager(config)
        providers = manager.get_providers_for_backend("qwen")

        assert len(providers) == 2
        assert providers[0].name == "provider1"
        assert providers[0].command == "uvx"
        assert providers[1].name == "provider2"
        assert providers[1].command == "python"

    def test_get_providers_for_backend_no_providers(self):
        """Test getting providers for a backend with no providers configured."""
        config = LLMBackendConfiguration()
        manager = BackendProviderManager(config)

        # Backend has no providers configured
        providers = manager.get_providers_for_backend("qwen")

        assert providers == []

    def test_get_providers_for_nonexistent_backend(self):
        """Test getting providers for a backend that doesn't exist."""
        config = LLMBackendConfiguration()
        manager = BackendProviderManager(config)

        providers = manager.get_providers_for_backend("nonexistent")

        assert providers == []

    def test_get_provider_config(self):
        """Test getting a specific provider configuration."""
        config = LLMBackendConfiguration()
        config.providers["test-provider"] = ProviderConfig(
            name="test-provider",
            command="uvx",
            description="Test provider",
            settings={"KEY": "value"},
        )

        manager = BackendProviderManager(config)
        provider = manager.get_provider_config("test-provider")

        assert provider is not None
        assert provider.name == "test-provider"
        assert provider.command == "uvx"
        assert provider.description == "Test provider"
        assert provider.settings["KEY"] == "value"

    def test_get_provider_config_nonexistent(self):
        """Test getting a provider that doesn't exist."""
        config = LLMBackendConfiguration()
        manager = BackendProviderManager(config)

        provider = manager.get_provider_config("nonexistent")

        assert provider is None

    def test_get_all_providers(self):
        """Test getting all provider configurations."""
        config = LLMBackendConfiguration()
        config.providers["provider1"] = ProviderConfig(name="provider1", command="uvx")
        config.providers["provider2"] = ProviderConfig(name="provider2", command="python")

        manager = BackendProviderManager(config)
        all_providers = manager.get_all_providers()

        assert len(all_providers) == 2
        assert "provider1" in all_providers
        assert "provider2" in all_providers
        assert all_providers["provider1"].command == "uvx"
        assert all_providers["provider2"].command == "python"

    def test_has_provider(self):
        """Test checking if a provider exists."""
        config = LLMBackendConfiguration()
        config.providers["existing-provider"] = ProviderConfig(name="existing-provider")

        manager = BackendProviderManager(config)

        assert manager.has_provider("existing-provider") is True
        assert manager.has_provider("nonexistent-provider") is False

    def test_get_provider_names_for_backend(self):
        """Test getting provider names for a backend."""
        config = LLMBackendConfiguration()
        backend_config = config.get_backend_config("qwen")
        backend_config.providers = ["provider1", "provider2", "provider3"]

        manager = BackendProviderManager(config)
        provider_names = manager.get_provider_names_for_backend("qwen")

        assert provider_names == ["provider1", "provider2", "provider3"]
        # Verify it's a copy, not the original
        provider_names.append("provider4")
        assert "provider4" not in backend_config.providers

    def test_get_provider_names_for_backend_no_providers(self):
        """Test getting provider names for a backend with no providers."""
        config = LLMBackendConfiguration()
        manager = BackendProviderManager(config)

        provider_names = manager.get_provider_names_for_backend("qwen")

        assert provider_names == []

    def test_get_provider_names_for_nonexistent_backend(self):
        """Test getting provider names for a nonexistent backend."""
        config = LLMBackendConfiguration()
        manager = BackendProviderManager(config)

        provider_names = manager.get_provider_names_for_backend("nonexistent")

        assert provider_names == []

    def test_get_provider_count_for_backend(self):
        """Test getting count of providers for a backend."""
        config = LLMBackendConfiguration()
        backend_config = config.get_backend_config("qwen")
        backend_config.providers = ["provider1", "provider2", "provider3"]

        manager = BackendProviderManager(config)
        count = manager.get_provider_count_for_backend("qwen")

        assert count == 3

    def test_get_provider_count_for_backend_no_providers(self):
        """Test getting count for a backend with no providers."""
        config = LLMBackendConfiguration()
        manager = BackendProviderManager(config)

        count = manager.get_provider_count_for_backend("qwen")

        assert count == 0

    def test_is_provider_usable(self):
        """Test checking if a provider is usable."""
        config = LLMBackendConfiguration()
        config.providers["test-provider"] = ProviderConfig(name="test-provider")

        manager = BackendProviderManager(config)

        assert manager.is_provider_usable("test-provider") is True
        assert manager.is_provider_usable("nonexistent-provider") is False

    def test_get_providers_ignores_nonexistent_provider_names(self):
        """Test that get_providers_for_backend ignores nonexistent provider names."""
        config = LLMBackendConfiguration()

        # Add only one provider
        config.providers["existing-provider"] = ProviderConfig(name="existing-provider", command="uvx")

        # Configure backend with one existing and one nonexistent provider
        backend_config = config.get_backend_config("qwen")
        backend_config.providers = ["existing-provider", "nonexistent-provider"]

        manager = BackendProviderManager(config)
        providers = manager.get_providers_for_backend("qwen")

        # Should only return the existing provider
        assert len(providers) == 1
        assert providers[0].name == "existing-provider"

    def test_manager_with_empty_config(self):
        """Test manager behavior with completely empty configuration."""
        config = LLMBackendConfiguration()
        manager = BackendProviderManager(config)

        # All methods should handle empty config gracefully
        assert manager.get_providers_for_backend("any-backend") == []
        assert manager.get_provider_config("any-provider") is None
        assert manager.get_all_providers() == {}
        assert manager.has_provider("any-provider") is False
        assert manager.get_provider_names_for_backend("any-backend") == []
        assert manager.get_provider_count_for_backend("any-backend") == 0
        assert manager.is_provider_usable("any-provider") is False

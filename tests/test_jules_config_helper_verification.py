"""
Verification tests for Issue #858: Sub-issue 1: Implement Jules Config Helper

This test file verifies:
1. LLMBackendConfiguration parses [jules] section correctly
2. get_backend_config("jules") returns the correct BackendConfig object
3. The 'enabled' property is correctly populated
"""

import os
import tempfile

from src.auto_coder.llm_backend_config import BackendConfig, LLMBackendConfiguration


def test_jules_section_parsing_verification():
    """
    Task 1: Verify that LLMBackendConfiguration parses [jules] section.

    This test verifies that when a TOML config file contains a [backends.jules]
    section, it is correctly parsed by LLMBackendConfiguration.load_from_file().
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = os.path.join(temp_dir, ".auto-coder", "llm_config.toml")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        # Create a config file with complete jules configuration
        with open(config_path, "w") as f:
            f.write(
                """
[backends.jules]
enabled = true
model = "jules-v1"
api_key = "test-api-key-123"
temperature = 0.5
timeout = 300
max_retries = 3
base_url = "https://jules.example.com"
"""
            )

        # Load and verify parsing
        config = LLMBackendConfiguration.load_from_file(config_path)

        assert config is not None, "Configuration should be loaded"
        assert "jules" in config.backends, "Jules backend should be in backends dict"

        jules_backend = config.backends["jules"]
        assert jules_backend.name == "jules", "Backend name should be 'jules'"
        assert jules_backend.enabled is True, "Enabled should be True"
        assert jules_backend.model == "jules-v1", "Model should be parsed correctly"
        assert jules_backend.api_key == "test-api-key-123", "API key should be parsed"
        assert jules_backend.temperature == 0.5, "Temperature should be parsed"
        assert jules_backend.timeout == 300, "Timeout should be parsed"
        assert jules_backend.max_retries == 3, "Max retries should be parsed"
        assert jules_backend.base_url == "https://jules.example.com", "Base URL should be parsed"

        print("✓ Task 1 VERIFIED: LLMBackendConfiguration correctly parses [jules] section")


def test_get_backend_config_returns_correct_object():
    """
    Task 2: Ensure get_backend_config("jules") returns the correct BackendConfig object.

    This test verifies that the get_backend_config() method returns a properly
    typed BackendConfig object with all expected attributes.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = os.path.join(temp_dir, ".auto-coder", "llm_config.toml")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        with open(config_path, "w") as f:
            f.write(
                """
[backends.jules]
enabled = true
model = "jules-model-v2"
"""
            )

        config = LLMBackendConfiguration.load_from_file(config_path)

        # Use the get_backend_config method
        jules_config = config.get_backend_config("jules")

        # Verify it's a BackendConfig object
        assert jules_config is not None, "get_backend_config should return a config"
        assert isinstance(jules_config, BackendConfig), "Should return BackendConfig instance"

        # Verify all BackendConfig attributes are accessible
        assert hasattr(jules_config, "name"), "Should have 'name' attribute"
        assert hasattr(jules_config, "enabled"), "Should have 'enabled' attribute"
        assert hasattr(jules_config, "model"), "Should have 'model' attribute"
        assert hasattr(jules_config, "api_key"), "Should have 'api_key' attribute"
        assert hasattr(jules_config, "base_url"), "Should have 'base_url' attribute"
        assert hasattr(jules_config, "temperature"), "Should have 'temperature' attribute"
        assert hasattr(jules_config, "timeout"), "Should have 'timeout' attribute"
        assert hasattr(jules_config, "max_retries"), "Should have 'max_retries' attribute"

        # Verify values are correct
        assert jules_config.name == "jules", "Name should be 'jules'"
        assert jules_config.model == "jules-model-v2", "Model should match config"

        print("✓ Task 2 VERIFIED: get_backend_config('jules') returns correct BackendConfig object")


def test_enabled_property_correctly_populated():
    """
    Task 3: Verify 'enabled' property is correctly populated.

    This test verifies that the 'enabled' property is correctly set based on
    configuration values, including:
    - True when explicitly set to true
    - False when explicitly set to false
    - True by default when not specified
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = os.path.join(temp_dir, ".auto-coder", "llm_config.toml")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        # Test Case 1: enabled = true
        with open(config_path, "w") as f:
            f.write(
                """
[backends.jules]
enabled = true
"""
            )

        config = LLMBackendConfiguration.load_from_file(config_path)
        jules_config = config.get_backend_config("jules")
        assert jules_config.enabled is True, "enabled should be True when set to true"

        # Test Case 2: enabled = false
        with open(config_path, "w") as f:
            f.write(
                """
[backends.jules]
enabled = false
"""
            )

        config = LLMBackendConfiguration.load_from_file(config_path)
        jules_config = config.get_backend_config("jules")
        assert jules_config.enabled is False, "enabled should be False when set to false"

        # Test Case 3: enabled not specified (default to true)
        with open(config_path, "w") as f:
            f.write(
                """
[backends.jules]
model = "jules-default"
"""
            )

        config = LLMBackendConfiguration.load_from_file(config_path)
        jules_config = config.get_backend_config("jules")
        assert jules_config.enabled is True, "enabled should default to True when not specified"

        # Test Case 4: No jules section at all (uses default backend)
        with open(config_path, "w") as f:
            f.write(
                """
[backends.codex]
enabled = true
"""
            )

        config = LLMBackendConfiguration.load_from_file(config_path)
        jules_config = config.get_backend_config("jules")
        assert jules_config is not None, "Jules should have default config even if not in file"
        assert jules_config.enabled is True, "Default jules backend should be enabled"

        print("✓ Task 3 VERIFIED: 'enabled' property is correctly populated in all cases")


def test_helper_integration_complete():
    """
    Integration test: Verify all three tasks work together correctly.

    This test demonstrates that the configuration system works end-to-end for
    the jules backend, meeting all requirements from issue #858.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = os.path.join(temp_dir, ".auto-coder", "llm_config.toml")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        # Create a realistic jules configuration
        with open(config_path, "w") as f:
            f.write(
                """
[backend]
order = ["codex", "jules", "gemini"]
default = "codex"

[backends.codex]
enabled = true
model = "codex"

[backends.jules]
enabled = true
model = "jules-production"
temperature = 0.7
timeout = 600
max_retries = 5

[backends.gemini]
enabled = false
"""
            )

        # Load configuration
        config = LLMBackendConfiguration.load_from_file(config_path)

        # Verify parsing (Task 1)
        assert "jules" in config.backends

        # Verify get_backend_config returns correct object (Task 2)
        jules_config = config.get_backend_config("jules")
        assert isinstance(jules_config, BackendConfig)
        assert jules_config.name == "jules"

        # Verify enabled property (Task 3)
        assert jules_config.enabled is True

        # Additional integration checks
        assert jules_config.model == "jules-production"
        assert jules_config.temperature == 0.7
        assert jules_config.timeout == 600
        assert jules_config.max_retries == 5

        # Verify jules is in backend order
        assert "jules" in config.backend_order
        assert config.backend_order.index("jules") == 1  # Second in order

        # Verify jules is in active backends (since it's enabled)
        active_backends = config.get_active_backends()
        assert "jules" in active_backends

        print("✓ INTEGRATION TEST PASSED: All tasks work together correctly")


def test_jules_default_model_helper():
    """
    Additional helper test: Verify get_model_for_backend() works for jules.

    This demonstrates that the helper method get_model_for_backend() correctly
    returns the model for jules backend, including fallback to default.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = os.path.join(temp_dir, ".auto-coder", "llm_config.toml")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        # Test with explicit model
        with open(config_path, "w") as f:
            f.write(
                """
[backends.jules]
enabled = true
model = "jules-custom-model"
"""
            )

        config = LLMBackendConfiguration.load_from_file(config_path)
        model = config.get_model_for_backend("jules")
        assert model == "jules-custom-model", "Should return configured model"

        # Test with default model (no model specified)
        with open(config_path, "w") as f:
            f.write(
                """
[backends.jules]
enabled = true
"""
            )

        config = LLMBackendConfiguration.load_from_file(config_path)
        model = config.get_model_for_backend("jules")
        # Should return the default model for jules backend
        assert model == "jules", "Should return default model 'jules' when not specified"

        print("✓ HELPER VERIFIED: get_model_for_backend() works correctly for jules")

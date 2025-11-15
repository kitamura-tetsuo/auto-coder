#!/usr/bin/env python3
"""
Simple test to verify the LLM backend configuration system works.
"""

import os
from pathlib import Path

from auto_coder.llm_backend_config import LLMBackendConfiguration


def test_config_loading() -> None:
    """Test that the configuration can be loaded and saved."""
    print("Testing LLM backend configuration system...")
    
    # Create a temporary config file for testing
    temp_config_path = Path("/tmp/test_llm_config.toml")
    
    # Create a sample configuration
    config = LLMBackendConfiguration()
    config.default_backend = "gemini"
    config.backend_order = ["gemini", "qwen", "claude"]
    config.message_default_backend = "qwen"
    config.message_backend_order = ["qwen", "gemini"]
    
    # Add a sample backend configuration
    from auto_coder.llm_backend_config import BackendConfig
    config.backends["gemini"] = BackendConfig(
        name="gemini",
        enabled=True,
        model="gemini-2.5-pro",
        api_key="test-key-123"
    )
    
    config.backends["qwen"] = BackendConfig(
        name="qwen",
        enabled=True,
        model="qwen3-coder-plus",
        openai_api_key="test-openai-key-123",
        openai_base_url="https://test-api.openai.com/v1"
    )
    
    # Save the configuration
    config.save_to_file(str(temp_config_path))
    print(f"Configuration saved to {temp_config_path}")
    
    # Load the configuration back
    loaded_config = LLMBackendConfiguration.load_from_file(str(temp_config_path))
    print("Configuration loaded successfully")
    
    # Verify the values
    assert loaded_config.default_backend == "gemini"
    assert loaded_config.backend_order == ["gemini", "qwen", "claude"]
    assert loaded_config.message_default_backend == "qwen"
    assert loaded_config.message_backend_order == ["qwen", "gemini"]
    
    gemini_config = loaded_config.get_backend_config("gemini")
    assert gemini_config is not None
    assert gemini_config.enabled == True
    assert gemini_config.model == "gemini-2.5-pro"
    assert gemini_config.api_key == "test-key-123"
    
    qwen_config = loaded_config.get_backend_config("qwen")
    assert qwen_config is not None
    assert qwen_config.enabled == True
    assert qwen_config.model == "qwen3-coder-plus"
    assert qwen_config.openai_api_key == "test-openai-key-123"
    assert qwen_config.openai_base_url == "https://test-api.openai.com/v1"
    
    print("All configuration tests passed!")
    
    # Test active backends
    active_backends = loaded_config.get_active_backends()
    print(f"Active backends: {active_backends}")
    assert active_backends == ["gemini", "qwen", "claude"]  # Based on order and enabled status
    
    # Test model retrieval
    gemini_model = loaded_config.get_model_for_backend("gemini")
    print(f"Gemini model: {gemini_model}")
    assert gemini_model == "gemini-2.5-pro"
    
    # Test default model for unknown backend
    unknown_model = loaded_config.get_model_for_backend("unknown")
    print(f"Unknown backend default model: {unknown_model}")
    assert unknown_model is None  # Should return None for unknown backends without defaults
    
    print("All tests passed successfully!")


if __name__ == "__main__":
    test_config_loading()
import os
import tempfile
from unittest.mock import patch, MagicMock
import pytest
import toml
from src.auto_coder.llm_backend_config import LLMBackendConfiguration, BackendConfig
from src.auto_coder.cli_helpers import create_failed_pr_backend_manager

class TestBackendConfigUpdate:
    def test_backend_order_default_inference(self):
        """Test that default backend is inferred from order[0]."""
        config_data = {
            "backend": {
                "order": ["claude", "codex"]
            },
            "backends": {
                "claude": {"enabled": True},
                "codex": {"enabled": True}
            }
        }
        
        config = LLMBackendConfiguration.load_from_dict(config_data)
        assert config.default_backend == "claude"
        
    def test_backend_noedit_order_default_inference(self):
        """Test that noedit default backend is inferred from order[0]."""
        config_data = {
            "backend": {
                "order": ["claude"]
            },
            "backend_for_noedit": {
                "order": ["gemini", "claude"]
            },
            "backends": {
                "claude": {"enabled": True},
                "gemini": {"enabled": True}
            }
        }
        
        config = LLMBackendConfiguration.load_from_dict(config_data)
        assert config.backend_for_noedit_default == "gemini"

    def test_backend_for_failed_pr_order(self):
        """Test parsing of backend_for_failed_pr order."""
        config_data = {
            "backend_for_failed_pr": {
                "order": ["codex", "claude"]
            },
            "backends": {
                "claude": {"enabled": True},
                "codex": {"enabled": True}
            }
        }
        
        config = LLMBackendConfiguration.load_from_dict(config_data)
        assert config.backend_for_failed_pr_order == ["codex", "claude"]

    @patch("src.auto_coder.cli_helpers.get_llm_config")
    @patch("src.auto_coder.cli_helpers.build_backend_manager")
    def test_create_failed_pr_backend_manager_with_order(self, mock_build, mock_get_config):
        """Test create_failed_pr_backend_manager uses order."""
        mock_config = MagicMock()
        mock_config.backend_for_failed_pr_order = ["codex", "claude"]
        mock_config.get_backend_for_failed_pr.return_value = None
        mock_config.get_model_for_backend.side_effect = lambda x: f"model-{x}"
        
        mock_get_config.return_value = mock_config
        
        create_failed_pr_backend_manager()
        
        mock_build.assert_called_once()
        call_args = mock_build.call_args[1]
        assert call_args["selected_backends"] == ["codex", "claude"]
        assert call_args["primary_backend"] == "codex"
        assert call_args["models"] == {"codex": "model-codex", "claude": "model-claude"}
        assert call_args["enable_graphrag"] is True

    def test_legacy_backend_for_failed_pr(self):
        """Test backward compatibility for backend_for_failed_pr as a config."""
        config_data = {
            "backend_for_failed_pr": {
                "name": "custom_failed_backend",
                "model": "gpt-4"
            }
        }
        
        config = LLMBackendConfiguration.load_from_dict(config_data)
        assert config.backend_for_failed_pr is not None
        assert config.backend_for_failed_pr.name == "custom_failed_backend"
        assert config.backend_for_failed_pr.model == "gpt-4"
        assert config.backend_for_failed_pr_order == []

    @patch("src.auto_coder.cli_helpers.get_llm_config")
    @patch("src.auto_coder.cli_helpers.build_backend_manager")
    def test_create_failed_pr_backend_manager_legacy(self, mock_build, mock_get_config):
        """Test create_failed_pr_backend_manager with legacy config."""
        mock_config = MagicMock()
        mock_config.backend_for_failed_pr_order = []
        
        mock_backend_config = MagicMock()
        mock_backend_config.name = "legacy_backend"
        mock_backend_config.model = "legacy_model"
        
        mock_config.get_backend_for_failed_pr.return_value = mock_backend_config
        
        mock_get_config.return_value = mock_config
        
        create_failed_pr_backend_manager()
        
        mock_build.assert_called_once()
        call_args = mock_build.call_args[1]
        assert call_args["selected_backends"] == ["legacy_backend"]
        assert call_args["primary_backend"] == "legacy_backend"
        assert call_args["models"] == {"legacy_backend": "legacy_model"}

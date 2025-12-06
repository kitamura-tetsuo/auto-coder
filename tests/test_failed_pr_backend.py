from unittest.mock import MagicMock, patch

import pytest

from auto_coder.cli_helpers import create_failed_pr_backend_manager
from auto_coder.llm_backend_config import BackendConfig


def test_create_failed_pr_backend_manager_no_config():
    with patch("auto_coder.cli_helpers.get_llm_config") as mock_get_config:
        mock_get_config.return_value.get_backend_for_failed_pr.return_value = None
        manager = create_failed_pr_backend_manager()
        assert manager is None


def test_create_failed_pr_backend_manager_with_config():
    with patch("auto_coder.cli_helpers.get_llm_config") as mock_get_config, patch("auto_coder.cli_helpers.build_backend_manager") as mock_build:

        mock_config = MagicMock()
        mock_backend_config = BackendConfig(name="failed_backend", model="failed_model")
        mock_config.get_backend_for_failed_pr.return_value = mock_backend_config
        # Set backend_for_failed_pr_order to empty list so it uses the legacy path
        mock_config.backend_for_failed_pr_order = []
        mock_get_config.return_value = mock_config

        mock_manager = MagicMock()
        mock_build.return_value = mock_manager

        manager = create_failed_pr_backend_manager()

        assert manager == mock_manager
        mock_build.assert_called_once()
        call_args = mock_build.call_args[1]
        assert call_args["selected_backends"] == ["failed_backend"]
        assert call_args["primary_backend"] == "failed_backend"
        assert call_args["models"] == {"failed_backend": "failed_model"}
        assert call_args["enable_graphrag"] is True

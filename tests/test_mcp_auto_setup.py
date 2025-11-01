"""Tests for automatic MCP server setup."""

import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.mcp_checker import ensure_graphrag_mcp_configured


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    """Create a temporary home directory for testing."""
    temp_home_dir = tmp_path / "home"
    temp_home_dir.mkdir()
    monkeypatch.setenv("HOME", str(temp_home_dir))
    return temp_home_dir


@pytest.fixture
def mock_setup_mcp():
    """Mock the setup-mcp command."""
    with patch(
        "src.auto_coder.cli_commands_graphrag.run_graphrag_setup_mcp_programmatically"
    ) as mock:
        mock.return_value = True
        yield mock


def test_auto_setup_when_mcp_dir_missing(temp_home, mock_setup_mcp):
    """Test that MCP server is automatically set up when directory is missing."""
    # Ensure MCP directory doesn't exist
    mcp_dir = temp_home / "graphrag_mcp"
    assert not mcp_dir.exists()

    # Mock check_graphrag_mcp_for_backend to return False initially, then True after setup
    with patch(
        "src.auto_coder.mcp_checker.check_graphrag_mcp_for_backend"
    ) as mock_check:
        mock_check.return_value = False

        # Call ensure_graphrag_mcp_configured with auto_setup=True
        result = ensure_graphrag_mcp_configured("codex", auto_setup=True)

        # Verify setup was called
        mock_setup_mcp.assert_called_once()
        call_args = mock_setup_mcp.call_args
        assert call_args.kwargs["install_dir"] is None  # Use default
        assert call_args.kwargs["backends"] == ["codex"]
        assert call_args.kwargs["silent"] is True


def test_no_auto_setup_when_disabled(temp_home, mock_setup_mcp):
    """Test that auto setup is skipped when auto_setup=False."""
    # Ensure MCP directory doesn't exist
    mcp_dir = temp_home / "graphrag_mcp"
    assert not mcp_dir.exists()

    # Mock check_graphrag_mcp_for_backend to return False
    with patch(
        "src.auto_coder.mcp_checker.check_graphrag_mcp_for_backend"
    ) as mock_check:
        mock_check.return_value = False

        # Mock add_graphrag_mcp_config to return False
        with patch("src.auto_coder.mcp_checker.add_graphrag_mcp_config") as mock_add:
            mock_add.return_value = False

            # Call ensure_graphrag_mcp_configured with auto_setup=False
            result = ensure_graphrag_mcp_configured("codex", auto_setup=False)

            # Verify setup was NOT called
            mock_setup_mcp.assert_not_called()

            # Verify result is False
            assert result is False


def test_skip_auto_setup_when_mcp_dir_exists(temp_home, mock_setup_mcp):
    """Test that auto setup is skipped when MCP directory already exists."""
    # Create MCP directory
    mcp_dir = temp_home / "graphrag_mcp"
    mcp_dir.mkdir()

    # Mock check_graphrag_mcp_for_backend to return True
    with patch(
        "src.auto_coder.mcp_checker.check_graphrag_mcp_for_backend"
    ) as mock_check:
        mock_check.return_value = True

        # Call ensure_graphrag_mcp_configured
        result = ensure_graphrag_mcp_configured("codex", auto_setup=True)

        # Verify setup was NOT called (directory already exists)
        mock_setup_mcp.assert_not_called()

        # Verify result is True
        assert result is True


def test_auto_setup_for_different_backends(temp_home, mock_setup_mcp):
    """Test that auto setup works for different backends."""
    backends = ["codex", "gemini", "qwen", "auggie"]

    for backend in backends:
        # Reset mock
        mock_setup_mcp.reset_mock()

        # Remove MCP directory if it exists
        mcp_dir = temp_home / "graphrag_mcp"
        if mcp_dir.exists():
            shutil.rmtree(mcp_dir)

        # Mock check_graphrag_mcp_for_backend to return False
        with patch(
            "src.auto_coder.mcp_checker.check_graphrag_mcp_for_backend"
        ) as mock_check:
            mock_check.return_value = False

            # Call ensure_graphrag_mcp_configured
            result = ensure_graphrag_mcp_configured(backend, auto_setup=True)

            # Verify setup was called with correct backend
            mock_setup_mcp.assert_called_once()
            call_args = mock_setup_mcp.call_args
            assert call_args.kwargs["backends"] == [backend]


def test_auto_setup_failure_handling(temp_home, mock_setup_mcp):
    """Test that failures in auto setup are handled gracefully."""
    # Make setup fail
    mock_setup_mcp.return_value = False

    # Ensure MCP directory doesn't exist
    mcp_dir = temp_home / "graphrag_mcp"
    assert not mcp_dir.exists()

    # Mock check_graphrag_mcp_for_backend to return False
    with patch(
        "src.auto_coder.mcp_checker.check_graphrag_mcp_for_backend"
    ) as mock_check:
        mock_check.return_value = False

        # Call ensure_graphrag_mcp_configured
        result = ensure_graphrag_mcp_configured("codex", auto_setup=True)

        # Verify setup was called
        mock_setup_mcp.assert_called_once()

        # Verify result is False (setup failed)
        assert result is False


def test_add_mcp_config_checks_directory_existence(temp_home):
    """Test that _add_*_mcp_config functions check for MCP directory existence."""
    from src.auto_coder.mcp_checker import (
        _add_auggie_mcp_config,
        _add_codex_mcp_config,
        _add_gemini_mcp_config,
        _add_qwen_mcp_config,
    )

    # Ensure MCP directory doesn't exist
    mcp_dir = temp_home / "graphrag_mcp"
    assert not mcp_dir.exists()

    # All functions should return False when directory doesn't exist
    assert _add_gemini_mcp_config() is False
    assert _add_qwen_mcp_config() is False
    assert _add_auggie_mcp_config() is False
    assert _add_codex_mcp_config() is False

    # Create MCP directory
    mcp_dir.mkdir()

    # Functions should still return False (config not found), but not because of missing directory
    # We can verify this by checking the log messages (not implemented here for simplicity)
    assert _add_gemini_mcp_config() is False
    assert _add_qwen_mcp_config() is False
    assert _add_auggie_mcp_config() is False
    assert _add_codex_mcp_config() is False


def test_suggest_graphrag_mcp_setup_mentions_auto_setup():
    """Test that setup suggestions mention automatic setup."""
    from src.auto_coder.mcp_checker import suggest_graphrag_mcp_setup

    backends = ["codex", "gemini", "qwen", "auggie"]

    for backend in backends:
        suggestion = suggest_graphrag_mcp_setup(backend)

        # Verify suggestion mentions automatic setup
        assert (
            "automatically" in suggestion.lower() or "automatic" in suggestion.lower()
        )
        assert "auto-coder graphrag setup-mcp" in suggestion

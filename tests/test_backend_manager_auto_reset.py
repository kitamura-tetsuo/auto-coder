"""Regression tests for persisted backend auto-reset behavior."""

from unittest.mock import MagicMock, patch

from auto_coder.backend_manager import BackendManager


def test_auto_reset_log_expands_backend_state_values(tmp_path):
    default_client = MagicMock()
    fallback_client = MagicMock()

    with patch("pathlib.Path.home", return_value=tmp_path):
        manager = BackendManager(
            default_backend="default",
            default_client=default_client,
            factories={
                "default": lambda: default_client,
                "fallback": lambda: fallback_client,
            },
            order=["default", "fallback"],
        )
        manager._state_manager.save_state("fallback", 100.0)

        with (
            patch("auto_coder.backend_manager.time.time", return_value=8000.0),
            patch("auto_coder.backend_manager.logger.info") as log_info,
        ):
            manager.check_and_reset_backend_if_needed()

    log_info.assert_called_once_with("Auto-resetting backend to default after 7900 seconds. " "Saved backend: fallback, Current backend: default")
    assert manager._current_backend_name() == "default"

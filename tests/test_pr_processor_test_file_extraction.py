import unittest
from unittest.mock import MagicMock, patch

from auto_coder.automation_config import AutomationConfig
from auto_coder.pr_processor import _apply_local_test_fix


class TestPRProcessorTestFileExtraction(unittest.TestCase):
    @patch("auto_coder.pr_processor.get_llm_backend_manager")
    @patch(
        "auto_coder.fix_to_pass_tests_runner.extract_important_errors_from_local_tests"
    )
    @patch("auto_coder.pr_processor.get_commit_log")
    @patch("auto_coder.pr_processor.render_prompt")
    @patch("auto_coder.pr_processor.extract_first_failed_test")
    def test_apply_local_test_fix_extracts_test_file(
        self,
        mock_extract_first_failed_test,
        mock_render_prompt,
        mock_get_commit_log,
        mock_extract_important_errors,
        mock_get_llm_backend_manager,
    ):
        # Arrange
        mock_llm_backend_manager = MagicMock()
        mock_get_llm_backend_manager.return_value = mock_llm_backend_manager

        mock_extract_important_errors.return_value = "Some error summary"
        mock_get_commit_log.return_value = "Some commit log"
        mock_render_prompt.return_value = "Some prompt"
        mock_extract_first_failed_test.return_value = "tests/test_failure.py"

        repo_name = "test/repo"
        pr_data = {"number": 123, "title": "Test PR"}
        config = AutomationConfig()
        test_result = {
            "success": False,
            "output": "some output",
            "errors": "some errors",
            "test_file": None,  # This is the key part of the test
        }
        attempt_history = []

        # Act
        _apply_local_test_fix(repo_name, pr_data, config, test_result, attempt_history)

        # Assert
        mock_extract_first_failed_test.assert_called_once_with("some output", "some errors")
        mock_llm_backend_manager.run_test_fix_prompt.assert_called_once_with("Some prompt", current_test_file="tests/test_failure.py")

    @patch("auto_coder.pr_processor.get_llm_backend_manager")
    @patch(
        "auto_coder.fix_to_pass_tests_runner.extract_important_errors_from_local_tests"
    )
    @patch("auto_coder.pr_processor.get_commit_log")
    @patch("auto_coder.pr_processor.render_prompt")
    @patch("auto_coder.pr_processor.extract_first_failed_test")
    def test_apply_local_test_fix_uses_existing_test_file(
        self,
        mock_extract_first_failed_test,
        mock_render_prompt,
        mock_get_commit_log,
        mock_extract_important_errors,
        mock_get_llm_backend_manager,
    ):
        # Arrange
        mock_llm_backend_manager = MagicMock()
        mock_get_llm_backend_manager.return_value = mock_llm_backend_manager

        mock_extract_important_errors.return_value = "Some error summary"
        mock_get_commit_log.return_value = "Some commit log"
        mock_render_prompt.return_value = "Some prompt"

        repo_name = "test/repo"
        pr_data = {"number": 123, "title": "Test PR"}
        config = AutomationConfig()
        test_result = {
            "success": False,
            "output": "some output",
            "errors": "some errors",
            "test_file": "tests/existing_test.py",
        }
        attempt_history = []

        # Act
        _apply_local_test_fix(repo_name, pr_data, config, test_result, attempt_history)

        # Assert
        mock_extract_first_failed_test.assert_not_called()
        mock_llm_backend_manager.run_test_fix_prompt.assert_called_once_with("Some prompt", current_test_file="tests/existing_test.py")


if __name__ == "__main__":
    unittest.main()

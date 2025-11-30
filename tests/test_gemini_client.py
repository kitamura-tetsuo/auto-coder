"""
Tests for Gemini client functionality.
"""

import json
from unittest.mock import Mock, patch

from src.auto_coder.gemini_client import GeminiClient
from src.auto_coder.utils import CommandResult


class TestGeminiClient:
    @patch("src.auto_coder.gemini_client.get_llm_config")
    @patch("src.auto_coder.gemini_client.logger")
    @patch("subprocess.run")
    @patch("src.auto_coder.gemini_client.CommandExecutor.run_command")
    def test_llm_invocation_warn_log(self, mock_run_command, mock_run, mock_logger, mock_get_config, mock_gemini_api_key):
        """Verify that LLM invocation emits a warning log before running CLI."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "ok\n", "", 0)

        # Mock config
        mock_config_instance = Mock()
        mock_backend_config = Mock()
        mock_backend_config.api_key = mock_gemini_api_key
        mock_config_instance.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config_instance

        client = GeminiClient()
        _ = client._run_llm_cli("hello")
        assert mock_logger.warning.called
        assert "LLM invocation" in str(mock_logger.warning.call_args[0][0])

    @patch("src.auto_coder.gemini_client.get_llm_config")
    @patch("src.auto_coder.gemini_client.logger")
    @patch("subprocess.run")
    @patch("src.auto_coder.gemini_client.CommandExecutor.run_command")
    def test_extra_args_are_passed_to_cli(self, mock_run_command, mock_run, mock_logger, mock_get_config, mock_gemini_api_key):
        """Resume or continuation flags should be forwarded to gemini CLI."""
        mock_run.return_value.returncode = 0
        mock_run_command.return_value = CommandResult(True, "ok\n", "", 0)

        # Mock config
        mock_config_instance = Mock()
        mock_backend_config = Mock()
        mock_backend_config.api_key = mock_gemini_api_key
        mock_backend_config.model = "test-model"
        mock_config_instance.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config_instance

        client = GeminiClient()
        client.set_extra_args(["--resume", "session42"])

        _ = client._run_llm_cli("ping")

        called_cmd = mock_run_command.call_args[0][0]
        assert called_cmd[-2:] == ["--prompt", "ping"]
        assert called_cmd[-4:-2] == ["--resume", "session42"]

    """Test cases for GeminiClient class."""

    @patch("src.auto_coder.gemini_client.get_llm_config")
    @patch("src.auto_coder.gemini_client.genai")
    def test_init(self, mock_genai, mock_get_config, mock_gemini_api_key):
        """Test GeminiClient initialization."""
        mock_model = Mock()
        mock_genai.GenerativeModel.return_value = mock_model

        # Mock config
        mock_config_instance = Mock()
        mock_backend_config = Mock()
        mock_backend_config.api_key = mock_gemini_api_key
        mock_backend_config.model = "test-model"
        mock_config_instance.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config_instance

        client = GeminiClient()

        assert client.api_key == mock_gemini_api_key
        assert client.model == mock_model
        mock_genai.configure.assert_called_once_with(api_key=mock_gemini_api_key)
        mock_genai.GenerativeModel.assert_called_once_with("test-model")

    @patch("src.auto_coder.gemini_client.get_llm_config")
    @patch("src.auto_coder.gemini_client.genai")
    def test_analyze_issue_removed(self, mock_genai, mock_get_config, mock_gemini_api_key):
        """Ensure analysis-only helpers are removed per LLM execution policy."""
        # Mock config
        mock_config_instance = Mock()
        mock_backend_config = Mock()
        mock_backend_config.api_key = mock_gemini_api_key
        mock_config_instance.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config_instance

        client = GeminiClient()
        assert not hasattr(client, "analyze_issue")

    @patch("src.auto_coder.gemini_client.get_llm_config")
    @patch("src.auto_coder.gemini_client.genai")
    def test_analyze_pull_request_removed(self, mock_genai, mock_get_config, mock_gemini_api_key, sample_pr_data):
        """Ensure analysis-only helpers are removed per LLM execution policy."""
        # Mock config
        mock_config_instance = Mock()
        mock_backend_config = Mock()
        mock_backend_config.api_key = mock_gemini_api_key
        mock_config_instance.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config_instance

        client = GeminiClient()
        assert not hasattr(client, "analyze_pull_request")

    @patch("src.auto_coder.gemini_client.get_llm_config")
    @patch("src.auto_coder.gemini_client.genai")
    def test_suggest_features_success(self, mock_genai, mock_get_config, mock_gemini_api_key):
        """Test successful feature suggestions."""
        # Setup
        mock_model = Mock()
        mock_response = Mock()
        mock_response.text = json.dumps(
            [
                {
                    "title": "Add authentication",
                    "description": "User authentication system",
                    "rationale": "Security requirement",
                    "priority": "high",
                    "complexity": "complex",
                    "estimated_effort": "weeks",
                    "labels": ["enhancement", "security"],
                    "acceptance_criteria": ["Users can login", "JWT tokens"],
                }
            ]
        )
        mock_model.generate_content.return_value = mock_response
        mock_genai.GenerativeModel.return_value = mock_model

        # Mock config
        mock_config_instance = Mock()
        mock_backend_config = Mock()
        mock_backend_config.api_key = mock_gemini_api_key
        mock_config_instance.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config_instance

        client = GeminiClient()
        repo_context = {"name": "test-repo", "description": "Test repository"}

        # Execute
        result = client.suggest_features(repo_context)

        # Assert
        assert len(result) == 1
        assert result[0]["title"] == "Add authentication"
        assert result[0]["priority"] == "high"
        assert "security" in result[0]["labels"]
        mock_model.generate_content.assert_called_once()

    @patch("src.auto_coder.gemini_client.get_llm_config")
    @patch("src.auto_coder.gemini_client.genai")
    def test_generate_solution_removed(self, mock_genai, mock_get_config, mock_gemini_api_key, sample_issue_data, sample_analysis_result):
        """Ensure generation helper is removed per LLM execution policy."""
        # Mock config
        mock_config_instance = Mock()
        mock_backend_config = Mock()
        mock_backend_config.api_key = mock_gemini_api_key
        mock_config_instance.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config_instance

        client = GeminiClient()
        assert not hasattr(client, "generate_solution")

    @patch("src.auto_coder.gemini_client.get_llm_config")
    def test_parse_analysis_response_valid_json(self, mock_get_config, mock_gemini_api_key):
        """Test parsing valid JSON response."""
        # Mock config
        mock_config_instance = Mock()
        mock_backend_config = Mock()
        mock_backend_config.api_key = mock_gemini_api_key
        mock_config_instance.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config_instance

        client = GeminiClient()

        response_text = """
        Here is the analysis:
        {
            "category": "bug",
            "priority": "high",
            "summary": "Test summary"
        }
        Additional text after JSON.
        """

        result = client._parse_analysis_response(response_text)

        assert result["category"] == "bug"
        assert result["priority"] == "high"
        assert result["summary"] == "Test summary"

    @patch("src.auto_coder.gemini_client.get_llm_config")
    def test_parse_analysis_response_invalid_json(self, mock_get_config, mock_gemini_api_key):
        """Test parsing invalid JSON response."""
        # Mock config
        mock_config_instance = Mock()
        mock_backend_config = Mock()
        mock_backend_config.api_key = mock_gemini_api_key
        mock_config_instance.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config_instance

        client = GeminiClient()

        response_text = "This is not JSON at all."

        result = client._parse_analysis_response(response_text)

        assert result["category"] == "unknown"
        assert result["priority"] == "medium"
        assert "This is not JSON" in result["summary"]

    @patch("src.auto_coder.gemini_client.get_llm_config")
    def test_parse_feature_suggestions_valid_json(self, mock_get_config, mock_gemini_api_key):
        """Test parsing valid feature suggestions JSON."""
        # Mock config
        mock_config_instance = Mock()
        mock_backend_config = Mock()
        mock_backend_config.api_key = mock_gemini_api_key
        mock_config_instance.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config_instance

        client = GeminiClient()

        response_text = """
        [
            {
                "title": "Feature 1",
                "description": "First feature"
            },
            {
                "title": "Feature 2",
                "description": "Second feature"
            }
        ]
        """

        result = client._parse_feature_suggestions(response_text)

        assert len(result) == 2
        assert result[0]["title"] == "Feature 1"
        assert result[1]["title"] == "Feature 2"

    @patch("src.auto_coder.gemini_client.get_llm_config")
    def test_parse_feature_suggestions_invalid_json(self, mock_get_config, mock_gemini_api_key):
        """Test parsing invalid feature suggestions JSON."""
        # Mock config
        mock_config_instance = Mock()
        mock_backend_config = Mock()
        mock_backend_config.api_key = mock_gemini_api_key
        mock_config_instance.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config_instance

        client = GeminiClient()

        response_text = "Not a JSON array"

        result = client._parse_feature_suggestions(response_text)

        assert result == []

    @patch("src.auto_coder.gemini_client.get_llm_config")
    def test_parse_solution_response_valid_json(self, mock_get_config, mock_gemini_api_key):
        """Test parsing valid solution response JSON."""
        # Mock config
        mock_config_instance = Mock()
        mock_backend_config = Mock()
        mock_backend_config.api_key = mock_gemini_api_key
        mock_config_instance.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config_instance

        client = GeminiClient()

        response_text = """
        {
            "solution_type": "code_fix",
            "summary": "Fix the bug",
            "steps": [],
            "code_changes": []
        }
        """

        result = client._parse_solution_response(response_text)

        assert result["solution_type"] == "code_fix"
        assert result["summary"] == "Fix the bug"
        assert result["steps"] == []
        assert result["code_changes"] == []

    @patch("src.auto_coder.gemini_client.get_llm_config")
    def test_parse_solution_response_invalid_json(self, mock_get_config, mock_gemini_api_key):
        """Test parsing invalid solution response JSON."""
        # Mock config
        mock_config_instance = Mock()
        mock_backend_config = Mock()
        mock_backend_config.api_key = mock_gemini_api_key
        mock_config_instance.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config_instance

        client = GeminiClient()

        response_text = "Invalid JSON response"

        result = client._parse_solution_response(response_text)

        assert result["solution_type"] == "investigation"
        assert "Invalid JSON" in result["summary"]
        assert result["steps"] == []
        assert result["code_changes"] == []

    @patch("src.auto_coder.gemini_client.get_llm_config")
    @patch("subprocess.run")
    def test_switch_to_conflict_model(self, mock_subprocess, mock_get_config):
        """Test switching to conflict resolution model."""
        # Mock subprocess for version check
        mock_subprocess.return_value.returncode = 0

        # Mock config
        mock_config_instance = Mock()
        mock_backend_config = Mock()
        mock_backend_config.model = "gemini-2.5-pro"
        mock_config_instance.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config_instance

        client = GeminiClient()

        # Initially should be using default model
        assert client.model_name == "gemini-2.5-pro"
        assert client.default_model == "gemini-2.5-pro"
        assert client.conflict_model == "gemini-2.5-flash"

        # Switch to conflict model
        client.switch_to_conflict_model()
        assert client.model_name == "gemini-2.5-flash"

        # Switch back to default
        client.switch_to_default_model()
        assert client.model_name == "gemini-2.5-pro"

    @patch("src.auto_coder.gemini_client.get_llm_config")
    @patch("subprocess.run")
    def test_switch_to_conflict_model_no_change_if_already_conflict(self, mock_subprocess, mock_get_config):
        """Test that switching to conflict model when already using it doesn't change anything."""
        # Mock subprocess for version check
        mock_subprocess.return_value.returncode = 0

        # Mock config
        mock_config_instance = Mock()
        mock_backend_config = Mock()
        mock_backend_config.model = "gemini-2.5-flash"
        mock_config_instance.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config_instance

        client = GeminiClient()

        # Should already be using conflict model
        assert client.model_name == "gemini-2.5-flash"

        # Switch to conflict model (should be no-op)
        client.switch_to_conflict_model()
        assert client.model_name == "gemini-2.5-flash"

    @patch("src.auto_coder.gemini_client.get_llm_config")
    @patch("subprocess.run")
    def test_escape_prompt_basic(self, mock_subprocess, mock_get_config):
        """Test basic @ character escaping in prompts."""
        # Mock subprocess for version check
        mock_subprocess.return_value.returncode = 0

        # Mock config
        mock_config_instance = Mock()
        mock_backend_config = Mock()
        mock_config_instance.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config_instance

        client = GeminiClient()

        # Test basic @ escaping
        prompt = "Please analyze @user's code"
        escaped = client._escape_prompt(prompt)
        assert escaped == "Please analyze \\@user's code"

    @patch("src.auto_coder.gemini_client.get_llm_config")
    @patch("subprocess.run")
    def test_escape_prompt_multiple_at_symbols(self, mock_subprocess, mock_get_config):
        """Test escaping multiple @ characters in prompts."""
        # Mock subprocess for version check
        mock_subprocess.return_value.returncode = 0

        # Mock config
        mock_config_instance = Mock()
        mock_backend_config = Mock()
        mock_config_instance.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config_instance

        client = GeminiClient()

        # Test multiple @ escaping
        prompt = "Check @user1 and @user2 mentions in @file"
        escaped = client._escape_prompt(prompt)
        assert escaped == "Check \\@user1 and \\@user2 mentions in \\@file"

    @patch("src.auto_coder.gemini_client.get_llm_config")
    @patch("subprocess.run")
    def test_escape_prompt_no_at_symbols(self, mock_subprocess, mock_get_config):
        """Test prompt without @ characters remains unchanged."""
        # Mock subprocess for version check
        mock_subprocess.return_value.returncode = 0

        # Mock config
        mock_config_instance = Mock()
        mock_backend_config = Mock()
        mock_config_instance.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config_instance

        client = GeminiClient()

        # Test no @ symbols
        prompt = "This is a normal prompt without special characters"
        escaped = client._escape_prompt(prompt)
        assert escaped == prompt

    @patch("src.auto_coder.gemini_client.get_llm_config")
    @patch("subprocess.run")
    def test_escape_prompt_empty_string(self, mock_subprocess, mock_get_config):
        """Test escaping empty string."""
        # Mock subprocess for version check
        mock_subprocess.return_value.returncode = 0

        # Mock config
        mock_config_instance = Mock()
        mock_backend_config = Mock()
        mock_config_instance.get_backend_config.return_value = mock_backend_config
        mock_get_config.return_value = mock_config_instance

        client = GeminiClient()

        # Test empty string
        prompt = ""
        escaped = client._escape_prompt(prompt)
        assert escaped == ""

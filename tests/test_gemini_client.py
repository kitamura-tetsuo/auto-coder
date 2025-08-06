"""
Tests for Gemini client functionality.
"""

import pytest
import json
from unittest.mock import Mock, patch, MagicMock

from src.auto_coder.gemini_client import GeminiClient


class TestGeminiClient:
    """Test cases for GeminiClient class."""
    
    @patch('src.auto_coder.gemini_client.genai')
    def test_init(self, mock_genai, mock_gemini_api_key):
        """Test GeminiClient initialization."""
        mock_model = Mock()
        mock_genai.GenerativeModel.return_value = mock_model
        
        client = GeminiClient(mock_gemini_api_key, "test-model")
        
        assert client.api_key == mock_gemini_api_key
        assert client.model == mock_model
        mock_genai.configure.assert_called_once_with(api_key=mock_gemini_api_key)
        mock_genai.GenerativeModel.assert_called_once_with("test-model")
    
    @patch('src.auto_coder.gemini_client.genai')
    def test_analyze_issue_success(self, mock_genai, mock_gemini_api_key, sample_issue_data):
        """Test successful issue analysis."""
        # Setup
        mock_model = Mock()
        mock_response = Mock()
        mock_response.text = json.dumps({
            "category": "bug",
            "priority": "high",
            "complexity": "moderate",
            "estimated_effort": "days",
            "tags": ["backend"],
            "recommendations": [{"action": "Fix bug", "rationale": "It's broken"}],
            "related_components": ["api"],
            "summary": "Test issue summary"
        })
        mock_model.generate_content.return_value = mock_response
        mock_genai.GenerativeModel.return_value = mock_model
        
        client = GeminiClient(mock_gemini_api_key)
        
        # Execute
        result = client.analyze_issue(sample_issue_data)
        
        # Assert
        assert result['category'] == 'bug'
        assert result['priority'] == 'high'
        assert result['complexity'] == 'moderate'
        assert len(result['recommendations']) == 1
        assert result['recommendations'][0]['action'] == 'Fix bug'
        mock_model.generate_content.assert_called_once()
    
    @patch('src.auto_coder.gemini_client.genai')
    def test_analyze_issue_with_error(self, mock_genai, mock_gemini_api_key, sample_issue_data):
        """Test issue analysis with error."""
        # Setup
        mock_model = Mock()
        mock_model.generate_content.side_effect = Exception("API Error")
        mock_genai.GenerativeModel.return_value = mock_model
        
        client = GeminiClient(mock_gemini_api_key)
        
        # Execute
        result = client.analyze_issue(sample_issue_data)
        
        # Assert
        assert result['error'] == 'API Error'
        assert result['category'] == 'analysis_error'
        assert result['priority'] == 'unknown'
    
    @patch('src.auto_coder.gemini_client.genai')
    def test_analyze_pull_request_success(self, mock_genai, mock_gemini_api_key, sample_pr_data):
        """Test successful PR analysis."""
        # Setup
        mock_model = Mock()
        mock_response = Mock()
        mock_response.text = json.dumps({
            "category": "feature",
            "risk_level": "low",
            "review_priority": "medium",
            "estimated_review_time": "hours",
            "recommendations": [{"action": "Review carefully", "rationale": "New feature"}],
            "potential_issues": ["None identified"],
            "summary": "Test PR summary"
        })
        mock_model.generate_content.return_value = mock_response
        mock_genai.GenerativeModel.return_value = mock_model
        
        client = GeminiClient(mock_gemini_api_key)
        
        # Execute
        result = client.analyze_pull_request(sample_pr_data)
        
        # Assert
        assert result['category'] == 'feature'
        assert result['risk_level'] == 'low'
        assert result['review_priority'] == 'medium'
        assert len(result['recommendations']) == 1
        mock_model.generate_content.assert_called_once()
    
    @patch('src.auto_coder.gemini_client.genai')
    def test_suggest_features_success(self, mock_genai, mock_gemini_api_key):
        """Test successful feature suggestions."""
        # Setup
        mock_model = Mock()
        mock_response = Mock()
        mock_response.text = json.dumps([
            {
                "title": "Add authentication",
                "description": "User authentication system",
                "rationale": "Security requirement",
                "priority": "high",
                "complexity": "complex",
                "estimated_effort": "weeks",
                "labels": ["enhancement", "security"],
                "acceptance_criteria": ["Users can login", "JWT tokens"]
            }
        ])
        mock_model.generate_content.return_value = mock_response
        mock_genai.GenerativeModel.return_value = mock_model
        
        client = GeminiClient(mock_gemini_api_key)
        repo_context = {"name": "test-repo", "description": "Test repository"}
        
        # Execute
        result = client.suggest_features(repo_context)
        
        # Assert
        assert len(result) == 1
        assert result[0]['title'] == 'Add authentication'
        assert result[0]['priority'] == 'high'
        assert 'security' in result[0]['labels']
        mock_model.generate_content.assert_called_once()
    
    @patch('src.auto_coder.gemini_client.genai')
    def test_generate_solution_success(self, mock_genai, mock_gemini_api_key, sample_issue_data, sample_analysis_result):
        """Test successful solution generation."""
        # Setup
        mock_model = Mock()
        mock_response = Mock()
        mock_response.text = json.dumps({
            "solution_type": "code_fix",
            "summary": "Fix the API endpoint",
            "steps": [
                {
                    "step": 1,
                    "description": "Update the endpoint",
                    "commands": ["git checkout -b fix-api"]
                }
            ],
            "code_changes": [
                {
                    "file": "api.py",
                    "action": "modify",
                    "description": "Fix endpoint logic",
                    "code": "def fixed_endpoint(): return 'fixed'"
                }
            ],
            "testing_strategy": "Unit tests",
            "risks": ["Breaking changes"]
        })
        mock_model.generate_content.return_value = mock_response
        mock_genai.GenerativeModel.return_value = mock_model
        
        client = GeminiClient(mock_gemini_api_key)
        
        # Execute
        result = client.generate_solution(sample_issue_data, sample_analysis_result)
        
        # Assert
        assert result['solution_type'] == 'code_fix'
        assert result['summary'] == 'Fix the API endpoint'
        assert len(result['steps']) == 1
        assert len(result['code_changes']) == 1
        assert result['code_changes'][0]['file'] == 'api.py'
        mock_model.generate_content.assert_called_once()
    
    def test_parse_analysis_response_valid_json(self, mock_gemini_api_key):
        """Test parsing valid JSON response."""
        client = GeminiClient(mock_gemini_api_key)
        
        response_text = '''
        Here is the analysis:
        {
            "category": "bug",
            "priority": "high",
            "summary": "Test summary"
        }
        Additional text after JSON.
        '''
        
        result = client._parse_analysis_response(response_text)
        
        assert result['category'] == 'bug'
        assert result['priority'] == 'high'
        assert result['summary'] == 'Test summary'
    
    def test_parse_analysis_response_invalid_json(self, mock_gemini_api_key):
        """Test parsing invalid JSON response."""
        client = GeminiClient(mock_gemini_api_key)
        
        response_text = "This is not JSON at all."
        
        result = client._parse_analysis_response(response_text)
        
        assert result['category'] == 'unknown'
        assert result['priority'] == 'medium'
        assert 'This is not JSON' in result['summary']
    
    def test_parse_feature_suggestions_valid_json(self, mock_gemini_api_key):
        """Test parsing valid feature suggestions JSON."""
        client = GeminiClient(mock_gemini_api_key)
        
        response_text = '''
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
        '''
        
        result = client._parse_feature_suggestions(response_text)
        
        assert len(result) == 2
        assert result[0]['title'] == 'Feature 1'
        assert result[1]['title'] == 'Feature 2'
    
    def test_parse_feature_suggestions_invalid_json(self, mock_gemini_api_key):
        """Test parsing invalid feature suggestions JSON."""
        client = GeminiClient(mock_gemini_api_key)
        
        response_text = "Not a JSON array"
        
        result = client._parse_feature_suggestions(response_text)
        
        assert result == []
    
    def test_parse_solution_response_valid_json(self, mock_gemini_api_key):
        """Test parsing valid solution response JSON."""
        client = GeminiClient(mock_gemini_api_key)
        
        response_text = '''
        {
            "solution_type": "code_fix",
            "summary": "Fix the bug",
            "steps": [],
            "code_changes": []
        }
        '''
        
        result = client._parse_solution_response(response_text)
        
        assert result['solution_type'] == 'code_fix'
        assert result['summary'] == 'Fix the bug'
        assert result['steps'] == []
        assert result['code_changes'] == []
    
    def test_parse_solution_response_invalid_json(self, mock_gemini_api_key):
        """Test parsing invalid solution response JSON."""
        client = GeminiClient(mock_gemini_api_key)
        
        response_text = "Invalid JSON response"
        
        result = client._parse_solution_response(response_text)
        
        assert result['solution_type'] == 'investigation'
        assert 'Invalid JSON' in result['summary']
        assert result['steps'] == []
        assert result['code_changes'] == []

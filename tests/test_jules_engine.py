import unittest
from unittest.mock import MagicMock, patch

from auto_coder.jules_engine import check_and_resume_or_archive_sessions


class TestJulesEngine(unittest.TestCase):
    @patch("auto_coder.jules_engine.JulesClient")
    @patch("auto_coder.jules_engine.GitHubClient")
    @patch("auto_coder.jules_engine._load_state")
    @patch("auto_coder.jules_engine._save_state")
    def test_delegates_to_process_session_status(self, mock_save_state, mock_load_state, mock_github_client_cls, mock_jules_client_cls):
        # Setup
        mock_jules_client = mock_jules_client_cls.return_value
        session1 = {"name": "projects/p/locations/l/sessions/s1", "state": "FAILED"}
        session2 = {"name": "projects/p/locations/l/sessions/s2", "state": "COMPLETED"}
        mock_jules_client.list_sessions.return_value = [session1, session2]
        
        mock_load_state.return_value = {"s1": 1}
        
        # Make process_session_status return True to trigger save_state
        mock_jules_client.process_session_status.return_value = True

        # Execute
        check_and_resume_or_archive_sessions()

        # Verify
        # Should call process_session_status for each session
        assert mock_jules_client.process_session_status.call_count == 2
        
        # Verify call args
        call_args_list = mock_jules_client.process_session_status.call_args_list
        
        # First call for session1
        args1, _ = call_args_list[0]
        assert args1[0] == session1
        assert args1[1] == {"s1": 1} # retry_state
        assert args1[2] is not None # github_client
        
        # Second call for session2
        args2, _ = call_args_list[1]
        assert args2[0] == session2
        # retry_state is passed by reference, so it's the same object
        assert args2[1] == {"s1": 1} 
        
        # Should save state
        mock_save_state.assert_called_once()

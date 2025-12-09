import logging
import sys
from unittest.mock import MagicMock, patch

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_jules_client_payload():
    print("Testing JulesClient payload construction...")
    
    # Mock get_llm_config
    with patch("auto_coder.jules_client.get_llm_config") as mock_get_config:
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.api_key = "test-api-key"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config
        
        from auto_coder.jules_client import JulesClient
        
        client = JulesClient()
        
        # Mock session.post to capture payload
        client.session.post = MagicMock()
        client.session.post.return_value.status_code = 200
        client.session.post.return_value.json.return_value = {"sessionId": "test-session-id"}
        
        # Call start_session
        repo_name = "test-owner/test-repo"
        base_branch = "main"
        prompt = "Fix bug"
        
        client.start_session(prompt, repo_name, base_branch)
        
        # Verify payload
        call_args = client.session.post.call_args
        if not call_args:
            print("FAILURE: session.post was not called.")
            sys.exit(1)
            
        kwargs = call_args[1]
        payload = kwargs.get("json")
        
        print(f"Payload: {payload}")
        
        if "sourceContext" not in payload:
            print("FAILURE: sourceContext missing from payload.")
            sys.exit(1)
            
        source_context = payload["sourceContext"]
        if source_context.get("source") != f"sources/github/{repo_name}":
            print(f"FAILURE: Incorrect source. Expected sources/github/{repo_name}, got {source_context.get('source')}")
            sys.exit(1)
            
        if source_context.get("githubRepoContext", {}).get("startingBranch") != base_branch:
            print(f"FAILURE: Incorrect startingBranch. Expected {base_branch}, got {source_context.get('githubRepoContext', {}).get('startingBranch')}")
            sys.exit(1)
            
        print("SUCCESS: Payload constructed correctly.")

if __name__ == "__main__":
    try:
        test_jules_client_payload()
        print("Verification passed!")
    except ImportError:
        print("FAILURE: Could not import auto_coder.jules_client. Installation might be incomplete.")
        sys.exit(1)
    except Exception as e:
        print(f"FAILURE: An error occurred: {e}")
        sys.exit(1)

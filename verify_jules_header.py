import logging
import sys
from unittest.mock import MagicMock, patch

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_jules_client_header():
    print("Testing JulesClient header configuration...")

    # Mock get_llm_config to return a config with an API key
    with patch("auto_coder.jules_client.get_llm_config") as mock_get_config:
        mock_config = MagicMock()
        mock_backend = MagicMock()
        mock_backend.api_key = "test-api-key"
        mock_config.get_backend_config.return_value = mock_backend
        mock_get_config.return_value = mock_config

        from auto_coder.jules_client import JulesClient

        client = JulesClient()

        # Check headers
        headers = client.session.headers
        print(f"Headers: {headers}")

        if "X-Goog-Api-Key" in headers and headers["X-Goog-Api-Key"] == "test-api-key":
             print("SUCCESS: X-Goog-Api-Key header is set correctly.")
        else:
             print("FAILURE: X-Goog-Api-Key header is NOT set correctly.")
             sys.exit(1)

        if "Authorization" in headers:
             print("WARNING: Authorization header is present. Ensure it is not conflicting.")
             if headers["Authorization"].startswith("Bearer test-api-key"):
                 print("FAILURE: Authorization header is still set to Bearer token with API key.")
                 sys.exit(1)

if __name__ == "__main__":
    try:
        test_jules_client_header()
        print("Verification passed!")
    except ImportError:
        print("FAILURE: Could not import auto_coder.jules_client. Installation might be incomplete.")
        sys.exit(1)
    except Exception as e:
        print(f"FAILURE: An error occurred: {e}")
        sys.exit(1)

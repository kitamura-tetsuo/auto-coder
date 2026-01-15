import logging
import sys
from unittest.mock import MagicMock, patch

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def test_jules_client_auth():
    print("Testing JulesClient authentication...")

    # Mock google.auth.default
    with patch("google.auth.default") as mock_default:
        # Case 1: ADC available
        mock_credentials = MagicMock()
        mock_default.return_value = (mock_credentials, "test-project")

        from auto_coder.jules_client import JulesClient

        client = JulesClient()

        if hasattr(client, "session") and hasattr(client.session, "credentials"):
            print("SUCCESS: JulesClient used AuthorizedSession when ADC is available.")
        else:
            print("FAILURE: JulesClient did NOT use AuthorizedSession when ADC is available.")
            sys.exit(1)

        # Case 2: ADC not available (exception)
        mock_default.side_effect = Exception("No credentials found")

        client_fallback = JulesClient()
        if hasattr(client_fallback, "session") and not hasattr(client_fallback.session, "credentials"):
            print("SUCCESS: JulesClient fell back to standard Session when ADC is missing.")
        else:
            print("FAILURE: JulesClient did NOT fall back to standard Session when ADC is missing.")
            sys.exit(1)


if __name__ == "__main__":
    try:
        test_jules_client_auth()
        print("Verification passed!")
    except ImportError:
        print("FAILURE: Could not import auto_coder.jules_client. Installation might be incomplete.")
        sys.exit(1)
    except Exception as e:
        print(f"FAILURE: An error occurred: {e}")
        sys.exit(1)

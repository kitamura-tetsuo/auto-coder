import logging
import os

import google.auth

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def check_auth():
    print("Checking Google Auth Environment...")

    # Check env vars
    print(f"GOOGLE_APPLICATION_CREDENTIALS: {os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')}")

    try:
        credentials, project = google.auth.default()
        print(f"SUCCESS: ADC found.")
        print(f"Project: {project}")
        print(f"Credentials type: {type(credentials)}")
        print(f"Service Account Email: {getattr(credentials, 'service_account_email', 'N/A')}")

        # Try to refresh to ensure they are valid
        from google.auth.transport.requests import Request

        credentials.refresh(Request())
        print("Credentials refreshed successfully.")
        print(f"Token: {credentials.token[:10]}..." if credentials.token else "No token yet")

    except Exception as e:
        print(f"FAILURE: ADC not found or invalid: {e}")


if __name__ == "__main__":
    check_auth()

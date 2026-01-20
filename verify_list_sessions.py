import logging
import os
import sys

# Add src to path
sys.path.append(os.path.join(os.getcwd(), "src"))

from auto_coder.jules_client import JulesClient
from auto_coder.logger_config import get_logger

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = get_logger(__name__)


def verify_list_sessions():
    try:
        logger.info("Initializing JulesClient...")
        client = JulesClient()

        logger.info("Calling list_sessions(page_size=5)...")
        sessions = client.list_sessions(page_size=5)

        logger.info(f"Successfully retrieved {len(sessions)} active sessions.")

        for i, session in enumerate(sessions[:3]):
            name = session.get("name", "UNKNOWN")
            state = session.get("state", "UNKNOWN")
            logger.info(f"Session {i+1}: {name} [{state}]")

        if len(sessions) > 3:
            logger.info(f"... and {len(sessions) - 3} more.")

    except Exception as e:
        logger.error(f"Verification failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    verify_list_sessions()

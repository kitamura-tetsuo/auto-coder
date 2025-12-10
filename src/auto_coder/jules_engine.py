"""
Jules engine module for managing Jules sessions.
"""

from .jules_client import JulesClient
from .logger_config import get_logger

logger = get_logger(__name__)


def check_and_resume_failed_sessions() -> None:
    """Check for failed Jules sessions and resume them."""
    try:
        jules_client = JulesClient()
        sessions = jules_client.list_sessions()
        for session in sessions:
            if session.get("state") == "FAILED":
                session_id = session.get("name", "").split("/")[-1]
                if not session_id:
                    session_id = session.get("id")
                
                if session_id:
                    logger.info(f"Resuming failed Jules session: {session_id}")
                    try:
                        jules_client.send_message(session_id, "ok")
                        logger.info(f"Successfully sent resume message to session {session_id}")
                    except Exception as e:
                        logger.error(f"Failed to resume session {session_id}: {e}")
    except Exception as e:
        logger.warning(f"Failed to check/resume Jules sessions: {e}")

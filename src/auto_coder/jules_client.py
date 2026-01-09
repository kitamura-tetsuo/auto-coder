"""
Jules HTTP API client for Auto-Coder.

Jules is a session-based AI assistant that can be used for issue processing.
This client uses HTTP API instead of Jules CLI to communicate with Jules.
"""

import json
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import requests  # type: ignore
from requests.adapters import HTTPAdapter  # type: ignore
from urllib3.util.retry import Retry

from .git_info import get_current_branch, get_current_repo_name
from .llm_backend_config import get_llm_config
from .llm_client_base import LLMClientBase
from .logger_config import get_logger

logger = get_logger(__name__)


class JulesClient(LLMClientBase):
    """Jules HTTP API client that manages session-based AI interactions."""

    def __init__(self, backend_name: Optional[str] = None) -> None:
        """Initialize Jules HTTP API client.

        Args:
            backend_name: Backend name to use for configuration lookup (optional).
        """
        self.backend_name = backend_name or "jules"
        self.timeout = None  # No timeout - let HTTP requests run as needed
        self.active_sessions: Dict[str, str] = {}  # Track active sessions
        self.current_session_id: Optional[str] = None  # Track the current session for continuous mode
        self.api_key: Optional[str] = None
        self.base_url = "https://jules.googleapis.com/v1alpha"

        # Load configuration for this backend
        config = get_llm_config()
        config_backend = config.get_backend_config(self.backend_name)
        self.options = (config_backend and config_backend.options) or []
        self.options_for_noedit = (config_backend and config_backend.options_for_noedit) or []
        self.api_key = (config_backend and config_backend.api_key) or None

        # Allow base_url override from configuration
        if config_backend and config_backend.base_url:
            self.base_url = config_backend.base_url

        # Check API connectivity
        if not self.api_key:
            logger.warning("No API key configured for Jules. API calls may fail.")

        # Create HTTP session with retry strategy
        self.session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        # Set headers
        self.session.headers.update({"Content-Type": "application/json", "User-Agent": "auto-coder/1.0"})
        if self.api_key:
            # Use X-Goog-Api-Key header instead of Authorization: Bearer
            # This is required for Jules API when using API keys
            self.session.headers["X-Goog-Api-Key"] = self.api_key

    def start_session(self, prompt: str, repo_name: str, base_branch: str, is_noedit: bool = False, title: Optional[str] = None) -> str:
        """Start a new Jules session with the given prompt.

        Args:
            prompt: The prompt to send to Jules
            repo_name: Repository name (e.g., 'owner/repo')
            base_branch: Base branch name (e.g., 'main')
            is_noedit: Whether this is a no-edit operation (uses options_for_noedit)
            title: Optional title for the session

        Returns:
            Session ID for the started session
        """
        try:
            # Prepare the request
            url = f"{self.base_url}/sessions"
            payload = {"prompt": prompt, "automationMode": "AUTO_CREATE_PR", "sourceContext": {"source": f"sources/github/{repo_name}", "githubRepoContext": {"startingBranch": base_branch}}}

            if title:
                payload["title"] = title

            logger.info("Starting Jules session")
            logger.info(f"🤖 POST {url}")
            logger.info("=" * 60)

            response = self.session.post(url, json=payload, timeout=self.timeout)

            logger.info("=" * 60)

            # Check if request was successful
            if response.status_code not in [200, 201]:
                error_msg = f"HTTP {response.status_code}: {response.text}"
                logger.error(f"Failed to start Jules session: {error_msg}")
                raise RuntimeError(f"Failed to start Jules session: {error_msg}")

            # Parse the response to get the session ID
            try:
                response_data = response.json()
                # Extract the session ID from the response
                # The exact field name depends on the API response format
                session_id = response_data.get("sessionId") or response_data.get("session_id") or response_data.get("id")
                if not session_id:
                    # Fallback: generate a session ID based on timestamp
                    session_id = f"session_{int(time.time())}"
                    logger.warning(f"Could not extract session ID from response, using generated ID: {session_id}")
            except json.JSONDecodeError:
                # Fallback: generate a session ID
                session_id = f"session_{int(time.time())}"
                logger.warning(f"Could not parse response JSON, using generated ID: {session_id}")

            # Track the session
            self.active_sessions[session_id] = prompt

            logger.info(f"Started Jules session: {session_id}")
            return session_id

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to start Jules session: {e}")
            raise RuntimeError(f"Failed to start Jules session: {e}")
        except Exception as e:
            logger.error(f"Failed to start Jules session: {e}")
            raise RuntimeError(f"Failed to start Jules session: {e}")

    def list_sessions(self, page_size: int = 20) -> List[Dict[str, Any]]:
        """List recent Jules sessions.

        Args:
            page_size: Number of sessions to return per page (default: 20)

        Returns:
            List of session dictionaries
        """
        try:
            # Prepare the request
            url = f"{self.base_url}/sessions"

            # Note: The API does not support server-side filtering for 'state'.
            # We must fetch sessions and filter them client-side.
            base_params = {
                "pageSize": page_size,
            }

            logger.info(f"Listing Jules sessions (pageSize={page_size})")

            all_sessions = []
            page_token = None

            while True:
                params = base_params.copy()
                if page_token:
                    params["pageToken"] = page_token

                logger.info(f"🤖 GET {url} (pageToken={page_token if page_token else 'None'})")

                response = self.session.get(url, params=params, timeout=self.timeout)

                # Check if request was successful
                if response.status_code != 200:
                    error_msg = f"HTTP {response.status_code}: {response.text}"
                    logger.error(f"Failed to list Jules sessions: {error_msg}")
                    raise RuntimeError(f"Failed to list Jules sessions: {error_msg}")

                # Parse the response
                try:
                    response_data = response.json()
                    sessions = response_data.get("sessions", [])
                    all_sessions.extend(sessions)

                    # Check for next page
                    page_token = response_data.get("nextPageToken")
                    if not page_token:
                        break

                except json.JSONDecodeError:
                    logger.error("Failed to parse Jules sessions response as JSON")
                    break

            # Filter out archived sessions client-side
            active_sessions = [s for s in all_sessions if s.get("state") != "ARCHIVED"]

            logger.info(f"Total sessions retrieved: {len(all_sessions)}, Active: {len(active_sessions)}")
            logger.info("=" * 60)
            return active_sessions

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to list Jules sessions: {e}")
            raise RuntimeError(f"Failed to list Jules sessions: {e}")
        except Exception as e:
            logger.error(f"Failed to list Jules sessions: {e}")
            raise RuntimeError(f"Failed to list Jules sessions: {e}")

    def send_message(self, session_id: str, message: str) -> str:
        """Send a message to an existing Jules session.

        Args:
            session_id: The session ID to send the message to
            message: The message to send

        Returns:
            Response from Jules
        """
        # Escape < / > to &lt; / &gt;
        message = message.replace("<", "&lt;").replace(">", "&gt;")

        try:
            # Prepare the request
            url = f"{self.base_url}/sessions/{session_id}:sendMessage"
            payload = {
                "prompt": message,
            }

            logger.info(f"Sending message to Jules session: {session_id}")
            logger.info(f"🤖 POST {url}")
            logger.info("=" * 60)

            response = self.session.post(url, json=payload, timeout=self.timeout)

            logger.info("=" * 60)

            # Check if request was successful
            if response.status_code != 200:
                error_msg = f"HTTP {response.status_code}: {response.text}"
                logger.error(f"Failed to send message to Jules session {session_id}: {error_msg}")
                raise RuntimeError(f"Failed to send message to Jules session {session_id}: {error_msg}")

            # Parse the response
            try:
                response_data = response.json()
                # Extract the response message
                # The exact field name depends on the API response format
                response_text = response_data.get("response") or response_data.get("message") or response_data.get("result") or str(response_data)
                return response_text
            except json.JSONDecodeError:
                # If response is not JSON, return the raw text
                return response.text

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to send message to Jules session {session_id}: {e}")
            raise RuntimeError(f"Failed to send message to Jules session {session_id}: {e}")
        except Exception as e:
            logger.error(f"Failed to send message to Jules session {session_id}: {e}")
            raise RuntimeError(f"Failed to send message to Jules session {session_id}: {e}")

    def end_session(self, session_id: str) -> bool:
        """End a Jules session.

        Args:
            session_id: The session ID to end

        Returns:
            True if session was ended successfully, False otherwise
        """
        try:
            # Prepare the request
            url = f"{self.base_url}/sessions/{session_id}"

            logger.info(f"Ending Jules session: {session_id}")
            logger.info(f"🤖 DELETE {url}")
            logger.info("=" * 60)

            response = self.session.delete(url, timeout=self.timeout)

            logger.info("=" * 60)

            # Check if request was successful
            if response.status_code == 200:
                # Remove from active sessions
                if session_id in self.active_sessions:
                    del self.active_sessions[session_id]

                logger.info(f"Ended Jules session: {session_id}")
                return True
            else:
                error_msg = f"HTTP {response.status_code}: {response.text}"
                logger.error(f"Failed to end Jules session {session_id}: {error_msg}")
                return False

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to end Jules session {session_id}: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to end Jules session {session_id}: {e}")
            return False

    def archive_session(self, session_id: str) -> bool:
        """Archive a Jules session.

        Args:
            session_id: The session ID to archive

        Returns:
            True if session was archived successfully, False otherwise
        """
        try:
            # Prepare the request
            url = f"{self.base_url}/sessions/{session_id}:archive"

            logger.info(f"Archiving Jules session: {session_id}")
            logger.info(f"🤖 POST {url}")
            logger.info("=" * 60)

            response = self.session.post(url, json={}, timeout=self.timeout)

            logger.info("=" * 60)

            # Check if request was successful
            if response.status_code == 200:
                # Remove from active sessions
                if session_id in self.active_sessions:
                    del self.active_sessions[session_id]

                logger.info(f"Archived Jules session: {session_id}")
                return True
            else:
                error_msg = f"HTTP {response.status_code}: {response.text}"
                logger.error(f"Failed to archive Jules session {session_id}: {error_msg}")
                return False

            return False
        except Exception as e:
            logger.error(f"Failed to archive Jules session {session_id}: {e}")
            return False

    def approve_plan(self, session_id: str) -> bool:
        """Approve a plan for a Jules session.

        Args:
            session_id: The session ID to approve the plan for

        Returns:
            True if plan was approved successfully, False otherwise
        """
        try:
            # Prepare the request
            url = f"{self.base_url}/sessions/{session_id}:approvePlan"

            logger.info(f"Approving plan for Jules session: {session_id}")
            logger.info(f"🤖 POST {url}")
            logger.info("=" * 60)

            response = self.session.post(url, json={}, timeout=self.timeout)

            logger.info("=" * 60)

            # Check if request was successful
            if response.status_code == 200:
                logger.info(f"Approved plan for Jules session: {session_id}")
                return True
            else:
                error_msg = f"HTTP {response.status_code}: {response.text}"
                logger.error(f"Failed to approve plan for Jules session {session_id}: {error_msg}")
                return False

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to approve plan for Jules session {session_id}: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to approve plan for Jules session {session_id}: {e}")
            return False

    def get_last_session_id(self) -> Optional[str]:
        """Get the ID of the last active session.

        Returns:
            The session ID if one exists, None otherwise.
        """
        return self.current_session_id

    def _get_pr_details(self, session: Dict[str, Any]) -> Tuple[Optional[str], Optional[int]]:
        """Extract PR repository and number from session outputs.

        Args:
            session: The session dictionary

        Returns:
            Tuple of (repo_name, pr_number) or (None, None)
        """
        outputs = session.get("outputs", {})
        
        # Normalize outputs if list
        if isinstance(outputs, list):
            try:
                new_outputs = {}
                for item in outputs:
                    if isinstance(item, dict):
                        new_outputs.update(item)
                    elif isinstance(item, (list, tuple)) and len(item) == 2:
                        new_outputs[item[0]] = item[1]
                outputs = new_outputs
            except Exception:
                outputs = {}

        pull_request = outputs.get("pullRequest")
        repo_name = None
        pr_number = None

        if isinstance(pull_request, dict):
            pr_number = pull_request.get("number")
            if "repository" in pull_request:
                repo_name = pull_request["repository"].get("name")
                if not repo_name and "full_name" in pull_request["repository"]:
                    repo_name = pull_request["repository"]["full_name"]

        elif isinstance(pull_request, str) and "github.com" in pull_request:
            parts = pull_request.split("/")
            if "pull" in parts:
                pull_idx = parts.index("pull")
                if pull_idx > 2 and pull_idx + 1 < len(parts):
                    repo_name = f"{parts[pull_idx-2]}/{parts[pull_idx-1]}"
                    try:
                        pr_number = int(parts[pull_idx + 1])
                    except ValueError:
                        pass
        
        return repo_name, pr_number

    def _run_llm_cli(self, prompt: str, is_noedit: bool = False) -> str:
        """Run Jules HTTP API with the given prompt and no return response.
        
        Orchestrates the entire session lifecycle:
        1. Starts a session (or reuses existing one)

        Args:
            prompt: The prompt to send
            is_noedit: Whether this is a no-edit operation (uses options_for_noedit)

        Returns:
            Fixed message that means to start session.
        """
        # Try to detect repo context from current environment
        repo_name = get_current_repo_name() or "unknown/repo"
        base_branch = get_current_branch() or "main"
        
        logger.info(f"_run_llm_cli called for JulesClient. Using inferred context: repo={repo_name}, branch={base_branch}")

        session_id = self.start_session(prompt, repo_name, base_branch, is_noedit)

        return session_id

    def _run_llm_cli_with_polling(self, prompt: str, is_noedit: bool = False) -> str:
        """Run Jules HTTP API with the given prompt and return response.
        
        Orchestrates the entire session lifecycle:
        1. Starts a session (or reuses existing one)
        2. Sends the initial prompt (or next message)
        3. Polls the session status until completion or PR creation
        4. Handles automated interactions (resume, approve plan, etc.)
        
        Args:
            prompt: The prompt to send
            is_noedit: Whether this is a no-edit operation (uses options_for_noedit)

        Returns:
            Final response or status message from Jules
        """
        # Try to detect repo context from current environment
        repo_name = get_current_repo_name() or "unknown/repo"
        base_branch = get_current_branch() or "main"
        
        logger.info(f"_run_llm_cli called for JulesClient. Using inferred context: repo={repo_name}, branch={base_branch}")

        # Check if we have an active session to reuse
        if self.current_session_id and not is_noedit:
             session_id = self.current_session_id
             logger.info(f"Reusing existing Jules session: {session_id}")
             try:
                 self.send_message(session_id, prompt)
             except Exception as e:
                 logger.warning(f"Failed to reuse session {session_id}, starting new one: {e}")
                 session_id = self.start_session(prompt, repo_name, base_branch, is_noedit)
                 self.current_session_id = session_id
        else:
             session_id = self.start_session(prompt, repo_name, base_branch, is_noedit)
             self.current_session_id = session_id
        
        # Initialize GitHub client for PR checks
        from .github_client import GitHubClient
        try:
            github_client = GitHubClient.get_instance()
        except ValueError:
            logger.warning("GitHubClient not initialized, PR status checks may be limited")
            github_client = None


        try:
            # Polling loop
            retry_state: Dict[str, int] = {}
            last_status = ""
            
            # Determine if we should auto-merge PRs
            # Auto-merge if we are NOT on main branch
            merge_pr = base_branch != "main"
            if merge_pr:
                logger.info(f"Auto-merge enabled for this session (base branch '{base_branch}' != 'main')")
            
            while True:
                time.sleep(60)  # Poll every minute as requested
                
                try:
                    session = self.get_session(session_id)
                    if not session:
                        logger.warning(f"Session {session_id} not found during polling")
                        break
                        
                    state = session.get("state")
                    if state != last_status:
                        logger.info(f"Session {session_id} status: {state}")
                        last_status = state or ""
                    
                    # Process session status (resume, approve, check PRs)
                    self.process_session_status(session, retry_state, github_client, merge_pr=merge_pr)
                    
                    # Check terminal conditions
                    if state == "ARCHIVED":
                        logger.info(f"Session {session_id} is archived. Work completed.")
                        return f"Session {session_id} completed and archived."
                    
                except Exception as e:
                    logger.error(f"Error during polling session {session_id}: {e}")
                    time.sleep(10) # Wait a bit before retrying

                # If auto-merge is enabled, check if we are done (PR merged)
                if merge_pr and github_client:
                    try:
                        session = self.get_session(session_id)
                        if session and session.get("state") == "COMPLETED":
                            repo, pr_number = self._get_pr_details(session)
                            if repo and pr_number:
                                repo_obj = github_client.get_repository(repo)
                                pr = repo_obj.get_pull(pr_number)
                                if pr.state == "closed" and pr.merged:
                                    logger.info(f"PR #{pr_number} merged. Returning control to caller.")
                                    return f"PR #{pr_number} merged. Session kept open for further interactions."
                    except Exception as e:
                         # Log but continue polling if check failed
                         logger.debug(f"Error checking PR status for exit condition: {e}")

                    
        finally:
            # We don't simple-end the session here because it might be long-running/async 
            # and handled by the polling loop/process_session_status.
            pass

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get a single Jules session by ID.

        Args:
            session_id: The session ID to retrieve

        Returns:
            Session dictionary if found, None otherwise
        """
        try:
            url = f"{self.base_url}/sessions/{session_id}"
            logger.debug(f"Getting session details: {session_id}")
            
            response = self.session.get(url, timeout=self.timeout)
            
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 404:
                logger.warning(f"Session {session_id} not found")
                return None
            else:
                logger.error(f"Failed to get session {session_id}: HTTP {response.status_code} {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"Failed to get session {session_id}: {e}")
            return None

    def process_session_status(self, session: Dict[str, Any], retry_state: Dict[str, int], github_client: Optional[Any] = None, merge_pr: bool = False) -> bool:
        """Process session status and take appropriate actions (resume, approve, archive).

        Args:
            session: The session dictionary
            retry_state: Dictionary tracking retry counts for sessions
            github_client: Optional GitHubClient instance for checking PR status
            merge_pr: Whether to automatically merge the PR if created (default: False)

        Returns:
            True if any action was taken (state changed), False otherwise
        """
        session_id = session.get("name", "").split("/")[-1]
        if not session_id:
            session_id = session.get("id") or ""
        
        if not session_id:
            return False

        state = session.get("state")
        outputs = session.get("outputs", {})
        
        # Normalize outputs if list
        if isinstance(outputs, list):
            try:
                new_outputs = {}
                for item in outputs:
                    if isinstance(item, dict):
                        new_outputs.update(item)
                    elif isinstance(item, (list, tuple)) and len(item) == 2:
                        new_outputs[item[0]] = item[1]
                outputs = new_outputs
            except Exception as e:
                logger.warning(f"Failed to convert list outputs to dict: {outputs} - {e}")
                outputs = {}

        pull_request = outputs.get("pullRequest")
        state_changed = False

        # Case 1: Failed session -> Resume
        if state == "FAILED":
            logger.info(f"Resuming failed Jules session: {session_id}")
            try:
                self.send_message(session_id, "ok")
                logger.info(f"Successfully sent resume message to session {session_id}")
                if session_id in retry_state:
                    del retry_state[session_id]
                state_changed = True
            except Exception as e:
                logger.error(f"Failed to resume session {session_id}: {e}")

        # Case 2: Awaiting Plan Approval -> Approve Plan
        elif state == "AWAITING_PLAN_APPROVAL":
            logger.info(f"Approving plan for Jules session: {session_id}")
            try:
                if self.approve_plan(session_id):
                    logger.info(f"Successfully approved plan for session {session_id}")
                    state_changed = True
                else:
                    logger.error(f"Failed to approve plan for session {session_id}")
            except Exception as e:
                logger.error(f"Failed to approve plan for session {session_id}: {e}")

        # Case 3: Completed session without PR -> Resume with retry logic
        elif state == "COMPLETED" and not pull_request:
            retry_count = retry_state.get(session_id, 0)
            
            if retry_count < 2:
                logger.info(f"Resuming completed Jules session (no PR) [Attempt {retry_count + 1}]: {session_id}")
                try:
                    self.send_message(session_id, "ok")
                    logger.info(f"Successfully sent resume message to session {session_id}")
                    retry_state[session_id] = retry_count + 1
                    state_changed = True
                except Exception as e:
                    logger.error(f"Failed to resume session {session_id}: {e}")
            else:
                logger.info(f"Resuming completed Jules session (no PR) [Force PR]: {session_id}")
                try:
                    self.send_message(session_id, "Please create a PR with the current code")
                    logger.info(f"Successfully sent force PR message to session {session_id}")
                    retry_state[session_id] = 0
                    state_changed = True
                except Exception as e:
                    logger.error(f"Failed to send force PR message to session {session_id}: {e}")

        # Case 4: Completed session with PR -> Check PR status and Archive if closed/merged (or auto-merge)
        elif state == "COMPLETED" and pull_request and github_client:
            if session_id in retry_state:
                del retry_state[session_id]
                state_changed = True

            try:
                repo_name, pr_number = self._get_pr_details(session)

                if repo_name and pr_number:
                    # Check PR status
                    repo = github_client.get_repository(repo_name)
                    pr = repo.get_pull(pr_number)

                    # Auto-merge logic
                    if merge_pr and pr.state == "open" and not pr.merged:
                        logger.info(f"Auto-merging PR #{pr_number} for session {session_id}")
                        try:
                            # Attempt to merge
                            merge_status = pr.merge()
                            if merge_status.merged:
                                logger.info(f"Successfully merged PR #{pr_number}")
                                # Reload PR to get updated state
                                pr = repo.get_pull(pr_number)
                                state_changed = True
                            else:
                                logger.warning(f"Failed to merge PR #{pr_number}: {merge_status.message}")
                        except Exception as e:
                            logger.error(f"Exception during auto-merge of PR #{pr_number}: {e}")

                    if pr.state == "closed":
                        # If merge_pr is True (continuous mode), we do NOT archive the session
                        if merge_pr:
                             logger.info(f"PR #{pr_number} is closed/merged. Keeping session {session_id} active for continuous mode.")
                             # Do NOT archive
                        else:
                            action = "merged" if pr.merged else "closed"
                            logger.info(f"PR #{pr_number} is {action}. Archiving Jules session: {session_id}")
                            if self.archive_session(session_id):
                                logger.info(f"Successfully archived session {session_id}")
                                state_changed = True
                            else:
                                logger.error(f"Failed to archive session {session_id}")
            except Exception as e:
                logger.warning(f"Failed to check PR status or archive session {session_id}: {e}")

        return state_changed

    def close(self) -> None:
        """Close the HTTP session and clean up resources."""
        if self.session:
            self.session.close()

    def check_mcp_server_configured(self, server_name: str) -> bool:
        """Check if a specific MCP server is configured for Jules CLI.

        Args:
            server_name: Name of the MCP server to check (e.g., 'graphrag', 'mcp-pdb')

        Returns:
            True if the MCP server is configured, False otherwise
        """
        # Jules doesn't support MCP servers like Claude/Gemini
        # Return False for all MCP server checks
        logger.debug(f"Jules does not support MCP servers. Check for '{server_name}' returned False")
        return False

    def add_mcp_server_config(self, server_name: str, command: str, args: list[str]) -> bool:
        """Add MCP server configuration for Jules CLI.

        Jules doesn't currently support MCP server configuration.

        Args:
            server_name: Name of the MCP server (e.g., 'graphrag', 'mcp-pdb')
            command: Command to run the MCP server (e.g., 'uv', '/path/to/script.sh')
            args: Arguments for the command (e.g., ['run', 'main.py'] or [])

        Returns:
            False - Jules does not support MCP server configuration
        """
        # Jules doesn't support MCP servers
        logger.debug(f"Jules does not support MCP server configuration. Cannot add '{server_name}'")
        return False

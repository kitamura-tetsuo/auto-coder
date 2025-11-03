"""
BackendManager: Manages multiple backends in rotation, handling usage limits and
automatic switching when the same current_test_file appears 3 consecutive times in apply_workspace_test_fix.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from .exceptions import AutoCoderUsageLimitError
from .llm_client_base import LLMBackendManagerBase
from .logger_config import get_logger, log_calls
from .progress_footer import ProgressStage

logger = get_logger(__name__)


class BackendManager(LLMBackendManagerBase):
    """Wrapper for managing LLM clients in circular rotation.

    - Provides _run_llm_cli(prompt) (client compatibility)
    - run_test_fix_prompt(prompt, current_test_file) is an extension for apply_workspace_test_fix:
      If the same model and current_test_file are given 3 consecutive times, rotate to the next backend.
      If a different current_test_file comes, reset to the default backend.
    - Each client is expected to throw AutoCoderUsageLimitError when reaching usage limits,
      and this triggers switching to the next backend for automatic retry.
    """

    # Class-level lock for thread-safe singleton
    _instance_lock = threading.Lock()

    # Singleton instance for message backend
    _message_instance: Optional["BackendManager"] = None

    def __init__(
        self,
        default_backend: str,
        default_client: Any,
        factories: Dict[str, Callable[[], Any]],
        order: Optional[List[str]] = None,
    ) -> None:
        # Backend order (circular)
        self._all_backends = order[:] if order else list(factories.keys())
        # Rotate default to front
        if default_backend in self._all_backends:
            while self._all_backends[0] != default_backend:
                self._all_backends.append(self._all_backends.pop(0))
        else:
            self._all_backends.insert(0, default_backend)
        # Client cache (lazy generation)
        self._factories = factories
        self._clients: Dict[str, Optional[Any]] = {k: None for k in self._all_backends}
        self._clients[default_backend] = default_client
        self._current_idx = 0
        self._default_backend = default_backend

        # Track recent prompt/model/backend for apply_workspace_test_fix
        self._last_prompt: Optional[str] = None
        self._last_backend: Optional[str] = None
        # Also record the most recently used model name to leave correct information in test CSV
        self._last_model: Optional[str] = getattr(default_client, "model_name", None)
        # Track current_test_file (switch if same file continues 3 times)
        self._last_test_file: Optional[str] = None
        self._same_test_file_count: int = 0

    # ---------- Basic Operations ----------
    def _current_backend_name(self) -> str:
        return self._all_backends[self._current_idx]

    def _get_or_create_client(self, name: str) -> Any:
        cli = self._clients.get(name)
        if cli is not None:
            return cli
        # Lazy generation
        fac = self._factories.get(name)
        if fac is None:
            raise RuntimeError(f"No factory for backend: {name}")
        try:
            cli = fac()
            self._clients[name] = cli
            return cli
        except Exception as e:
            # Skip if unable to generate (proceed to next)
            raise RuntimeError(f"Failed to initialize backend '{name}': {e}")

    def _switch_to_index(self, idx: int) -> None:
        self._current_idx = idx % len(self._all_backends)
        try:
            # Model switching linkage: restore to default
            cli = self._get_or_create_client(self._current_backend_name())
            try:
                cli.switch_to_default_model()
            except Exception:
                pass
        except Exception:
            pass

    def switch_to_next_backend(self) -> None:
        self._switch_to_index(self._current_idx + 1)
        logger.info(
            f"BackendManager: switched to next backend -> {self._current_backend_name()}"
        )

    def switch_to_default_backend(self) -> None:
        # To the default position
        try:
            idx = self._all_backends.index(self._default_backend)
        except ValueError:
            idx = 0
        self._switch_to_index(idx)
        logger.info(
            f"BackendManager: switched back to default backend -> {self._current_backend_name()}"
        )

    # ---------- Direct Compatibility Methods ----------
    @log_calls
    def _run_llm_cli(self, prompt: str) -> str:
        """Normal execution (circular retry on usage limit)."""
        attempts = 0
        tried: set[int] = set()
        last_error: Optional[Exception] = None
        while attempts < len(self._all_backends):
            name = self._current_backend_name()
            with ProgressStage(f"Running LLM: {name}, attempt {attempts + 1}"):
                if self._current_idx in tried:
                    self.switch_to_next_backend()
                    attempts += 1
                    continue
                tried.add(self._current_idx)
                try:
                    cli = self._get_or_create_client(name)
                    out = cli._run_llm_cli(prompt)
                    # Only on successful execution: update the most recently used backend/model
                    self._last_backend = name
                    self._last_model = getattr(cli, "model_name", None)
                    return out
                except AutoCoderUsageLimitError as e:
                    logger.warning(
                        f"Backend '{name}' hit usage limit: {e}. Rotating to next backend."
                    )
                    last_error = e
                    self.switch_to_next_backend()
                    attempts += 1
                    continue
                except Exception as e:
                    # Other failures propagate (immediate error except usage limit)
                    last_error = e
                    break
        if last_error:
            raise last_error
        raise RuntimeError("No backend available to run prompt")

    # ---------- For apply_workspace_test_fix ----------
    @log_calls
    def run_test_fix_prompt(
        self, prompt: str, current_test_file: Optional[str] = None
    ) -> str:
        """Execution for apply_workspace_test_fix.
        - If the same current_test_file is given 3 consecutive times, switch to the next backend
        - If a different current_test_file comes, reset to default
        - Then call _run_llm_cli (further rotation on usage limit)
        """
        # Get current backend and model name
        current_backend = self._current_backend_name()

        if self._last_test_file is None or current_test_file != self._last_test_file:
            # test_file changed -> reset to default (this is the 1st time)
            self.switch_to_default_backend()
            self._same_test_file_count = 1
        else:
            # Same test_file
            if self._last_backend == current_backend:
                # If the same backend continued twice before, switch before the 3rd execution
                if self._same_test_file_count >= 2:
                    self.switch_to_next_backend()
                    self._same_test_file_count = 1
                else:
                    self._same_test_file_count += 1
            else:
                # Backend has changed, reset counter (this is the 1st time)
                self._same_test_file_count = 1

        # Execute
        with ProgressStage(f"Running LLM: {self._current_backend_name()}"):
            out = self._run_llm_cli(prompt)

        # Update state
        self._last_prompt = prompt
        self._last_test_file = current_test_file
        self._last_backend = self._current_backend_name()
        return out

    def get_last_backend_and_model(self) -> Tuple[Optional[str], Optional[str]]:
        """Return the backend/model used for the most recent execution."""

        backend = self._last_backend or self._current_backend_name()
        model = self._last_model
        if model is None:
            try:
                cli = self._get_or_create_client(self._current_backend_name())
                model = getattr(cli, "model_name", None)
            except Exception:
                model = None
        return backend, model

    # ---------- Compatibility Helpers ----------
    def switch_to_conflict_model(self) -> None:
        try:
            cli = self._get_or_create_client(self._current_backend_name())
            if hasattr(cli, "switch_to_conflict_model") and callable(
                getattr(cli, "switch_to_conflict_model")
            ):
                cli.switch_to_conflict_model()
        except Exception:
            pass

    def switch_to_default_model(self) -> None:
        try:
            cli = self._get_or_create_client(self._current_backend_name())
            cli.switch_to_default_model()
        except Exception:
            pass

    def close(self) -> None:
        """Call client's close if available."""
        for _, cli in list(self._clients.items()):
            try:
                if cli:
                    cli.close()
            except Exception:
                pass

    def check_mcp_server_configured(self, server_name: str) -> bool:
        """Check if a specific MCP server is configured for the current backend.

        Args:
            server_name: Name of the MCP server to check (e.g., 'graphrag', 'mcp-pdb')

        Returns:
            True if the MCP server is configured, False otherwise
        """
        cli = self._get_or_create_client(self._current_backend_name())
        return cli.check_mcp_server_configured(server_name)

    def add_mcp_server_config(
        self, server_name: str, command: str, args: list[str]
    ) -> bool:
        """Add MCP server configuration for the current backend.

        Args:
            server_name: Name of the MCP server (e.g., 'graphrag', 'mcp-pdb')
            command: Command to run the MCP server (e.g., 'uv', '/path/to/script.sh')
            args: Arguments for the command (e.g., ['run', 'main.py'] or [])

        Returns:
            True if configuration was added successfully, False otherwise
        """
        cli = self._get_or_create_client(self._current_backend_name())
        return cli.add_mcp_server_config(server_name, command, args)

    def ensure_mcp_server_configured(
        self, server_name: str, command: str, args: list[str]
    ) -> bool:
        """Ensure a specific MCP server is configured for all backends, adding it if necessary.

        This method checks if the server is configured for each backend,
        and if not, adds the configuration.

        Args:
            server_name: Name of the MCP server (e.g., 'graphrag', 'mcp-pdb')
            command: Command to run the MCP server (e.g., 'uv', '/path/to/script.sh')
            args: Arguments for the command (e.g., ['run', 'main.py'] or [])

        Returns:
            True if the MCP server is configured (or was successfully added) for all backends, False otherwise
        """
        all_success = True
        for backend_name in self._all_backends:
            try:
                cli = self._get_or_create_client(backend_name)
                # Use the client's ensure_mcp_server_configured method
                # which handles check and add internally
                if not cli.ensure_mcp_server_configured(server_name, command, args):
                    all_success = False

            except Exception as e:
                logger.error(
                    f"Error configuring MCP server '{server_name}' for backend '{backend_name}': {e}"
                )
                all_success = False

        return all_success

    @classmethod
    def get_llm_for_message_instance(
        cls,
        default_backend: str,
        default_client: Any,
        factories: Dict[str, Callable[[], Any]],
        order: Optional[List[str]] = None,
    ) -> "BackendManager":
        """Get or create a singleton instance for message backend.

        This method returns a singleton instance optimized for message generation
        (commit messages, PR descriptions, etc.) using lightweight models.

        Args:
            default_backend: Name of the default backend
            default_client: Default client instance
            factories: Dictionary of backend name to factory function
            order: Optional list specifying the order of backends for rotation

        Returns:
            BackendManager singleton instance for message generation

        Note:
            This singleton is specifically designed for message generation tasks
            and uses lightweight models to reduce costs and improve response times.
        """
        if cls._message_instance is None:
            with cls._instance_lock:
                # Double-check locking pattern
                if cls._message_instance is None:
                    logger.info("Creating singleton instance for message backend")
                    cls._message_instance = cls(
                        default_backend=default_backend,
                        default_client=default_client,
                        factories=factories,
                        order=order,
                    )
        return cls._message_instance

    @classmethod
    def reset_message_instance(cls) -> None:
        """Reset the singleton instance (useful for testing)."""
        with cls._instance_lock:
            if cls._message_instance is not None:
                logger.info("Resetting message backend singleton instance")
                cls._message_instance.close()
                cls._message_instance = None

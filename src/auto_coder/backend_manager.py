"""
BackendManager: Manages multiple backends in rotation, handling usage limits and
automatic switching when the same current_test_file appears 3 consecutive times in apply_workspace_test_fix.
"""

from __future__ import annotations

import contextlib
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from .backend_provider_manager import BackendProviderManager
from .exceptions import AutoCoderUsageLimitError
from .llm_backend_config import LLMBackendConfiguration, get_llm_config
from .llm_client_base import LLMBackendManagerBase
from .logger_config import get_logger, log_calls
from .progress_footer import ProgressStage

logger = get_logger(__name__)

# Global singleton instance for general LLM operations
_llm_instance: Optional[BackendManager] = None
_message_instance: Optional[BackendManager] = None
_instance_lock = threading.Lock()
_initialization_lock = threading.Lock()

# Explicit exports for mypy
__all__ = [
    "BackendManager",
    "get_llm_backend_manager",
    "run_llm_message_prompt",
    "run_llm_prompt",
    "get_llm_backend_and_model",
    "get_llm_backend_provider_and_model",
    "LLMBackendManager",
    "get_message_backend_manager",
    "get_message_backend_and_model",
]


class BackendManager(LLMBackendManagerBase):
    """Wrapper for managing LLM clients in circular rotation.

    - Provides _run_llm_cli(prompt) (client compatibility)
    - run_test_fix_prompt(prompt, current_test_file) is an extension for apply_workspace_test_fix:
      If the same model and current_test_file are given 3 consecutive times, rotate to the next backend.
      If a different current_test_file comes, reset to the default backend.
    - Each client is expected to throw AutoCoderUsageLimitError when reaching usage limits,
      and this triggers switching to the next backend for automatic retry.
    """

    def __init__(
        self,
        default_backend: str,
        default_client: Any,
        factories: Dict[str, Callable[[], Any]],
        order: Optional[List[str]] = None,
        provider_manager: Optional[BackendProviderManager] = None,
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
        # Initialize _last_backend to current backend for testing purposes
        self._last_backend: Optional[str] = default_backend
        # Also record the most recently used model name to leave correct information in test CSV
        # Initialize from default client if available
        self._last_model: Optional[str] = getattr(default_client, "model_name", None)
        # If model_name is not available from client, try to get it from the client directly
        if self._last_model is None and hasattr(default_client, "get_last_backend_and_model"):
            # Some clients might have this method to get backend info
            try:
                backend, model = default_client.get_last_backend_and_model()
                self._last_model = model
            except Exception:
                self._last_model = None
        # Track current_test_file (switch if same file continues 3 times)
        self._last_test_file: Optional[str] = None
        self._same_test_file_count: int = 0

        # Provider manager for backend provider metadata
        # Implements provider rotation logic for switching between different provider implementations
        # for the same backend (e.g., qwen-open-router vs qwen-azure vs qwen-direct)
        self._provider_manager: BackendProviderManager = provider_manager or BackendProviderManager.get_default_manager()

    @property
    def provider_manager(self) -> BackendProviderManager:
        """
        Get the provider manager for this backend manager.

        Returns:
            BackendProviderManager: The provider manager instance

        Note: The provider manager implements provider rotation logic, including
        automatic failover when AutoCoderUsageLimitError occurs, environment
        variable handling for provider-specific configuration, and tracking of
        last used providers for debugging and telemetry.
        """
        return self._provider_manager

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
        logger.info(f"BackendManager: switched to next backend -> {self._current_backend_name()}")

    def switch_to_default_backend(self) -> None:
        # To the default position
        try:
            idx = self._all_backends.index(self._default_backend)
        except ValueError:
            idx = 0
        self._switch_to_index(idx)
        logger.info(f"BackendManager: switched back to default backend -> {self._current_backend_name()}")

    def _get_current_provider_name(self, backend_name: str) -> Optional[str]:
        """
        Get the current provider name for a backend.

        Args:
            backend_name: Name of the backend

        Returns:
            Current provider name, or None if no providers configured
        """
        return self._provider_manager.get_current_provider_name(backend_name)

    # ---------- Direct Compatibility Methods ----------
    @log_calls  # type: ignore[misc]
    def _run_llm_cli(self, prompt: str) -> str:
        """Normal execution (circular retry on usage limit with provider rotation)."""
        from .utils import TemporaryEnvironment

        attempts = 0
        tried: set[int] = set()
        last_error: Optional[Exception] = None

        while attempts < len(self._all_backends):
            if self._current_idx in tried:
                self.switch_to_next_backend()
                continue

            tried.add(self._current_idx)
            backend_name = self._current_backend_name()

            try:
                cli = self._get_or_create_client(backend_name)
            except Exception as exc:  # pragma: no cover - defensive guard
                last_error = exc
                logger.error(f"Failed to initialize backend '{backend_name}': {exc}")
                self.switch_to_next_backend()
                attempts += 1
                continue

            try:
                return self._execute_backend_with_providers(
                    backend_name=backend_name,
                    cli=cli,
                    prompt=prompt,
                    backend_attempt_number=attempts + 1,
                    temp_env_cls=TemporaryEnvironment,
                )
            except AutoCoderUsageLimitError as exc:
                last_error = exc
                logger.warning(f"Backend '{backend_name}' exhausted providers due to usage limits: {exc}")
                self.switch_to_next_backend()
                attempts += 1
                continue
            except Exception as exc:
                last_error = exc
                logger.error(f"Execution failed on backend '{backend_name}': {exc}")
                break

        if last_error:
            raise last_error
        raise RuntimeError("No backend available to run prompt")

    def _execute_backend_with_providers(
        self,
        backend_name: str,
        cli: Any,
        prompt: str,
        backend_attempt_number: int,
        temp_env_cls: Callable[[Dict[str, str]], Any],
    ) -> str:
        """
        Execute a backend client while honoring provider rotation rules.

        Args:
            backend_name: Name of the backend being executed
            cli: Backend client instance
            prompt: Prompt to execute
            backend_attempt_number: 1-based attempt number for logging context
            temp_env_cls: Context manager factory (injected for easier testing)
        """
        backend_has_providers = self._provider_manager.has_providers(backend_name)
        provider_count = self._provider_manager.get_provider_count(backend_name)
        provider_attempts = 0

        while True:
            provider_name = self._get_current_provider_name(backend_name)
            env_vars = self._provider_manager.create_env_context(backend_name) if backend_has_providers else {}
            provider_context = f"{backend_name}"
            if provider_name:
                provider_context += f" (provider: {provider_name})"
            message = f"Running LLM: {provider_context}, attempt {backend_attempt_number}"

            env_context = temp_env_cls(env_vars) if env_vars else contextlib.nullcontext()

            with ProgressStage(message), env_context:
                try:
                    out: str = cli._run_llm_cli(prompt)
                    self._last_backend = backend_name
                    self._last_model = getattr(cli, "model_name", None)
                    self._provider_manager.mark_provider_used(backend_name, provider_name)
                    logger.info(f"Successfully executed on backend '{backend_name}' " f"(provider: '{provider_name or 'default'}')")
                    return out
                except AutoCoderUsageLimitError as exc:
                    logger.warning(f"Provider usage limit reached for backend '{backend_name}' " f"(provider: '{provider_name or 'default'}'): {exc}")
                    if backend_has_providers and provider_count > 1 and provider_attempts < provider_count - 1:
                        rotated = self._provider_manager.advance_to_next_provider(backend_name)
                        if rotated:
                            provider_attempts += 1
                            continue
                    raise

    # ---------- For apply_workspace_test_fix ----------
    @log_calls  # type: ignore[misc]
    def run_test_fix_prompt(self, prompt: str, current_test_file: Optional[str] = None) -> str:
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

        # Execute with provider-aware telemetry so logs show which provider is being used.
        active_backend = self._current_backend_name()
        provider_for_stage = self._get_current_provider_name(active_backend)
        stage_label = f"Running LLM: {active_backend}"
        if provider_for_stage:
            stage_label += f" (provider: {provider_for_stage})"

        with ProgressStage(stage_label):
            out: str = self._run_llm_cli(prompt)

        # Update state
        self._last_prompt = prompt
        self._last_test_file = current_test_file
        self._last_backend = self._current_backend_name()
        return out

    def get_last_backend_and_model(self) -> Tuple[Optional[str], Optional[str]]:
        """Return the backend/model used for the most recent execution."""

        # Get current backend (last used or current)
        backend = self._last_backend or self._current_backend_name()

        # Get model from last execution or from current client
        model = self._last_model
        if model is None:
            try:
                # Get model from current backend client
                current_backend = self._current_backend_name()
                cli = self._get_or_create_client(current_backend)
                model = getattr(cli, "model_name", None)
            except Exception:
                model = None
        return backend, model

    def get_last_backend_provider_and_model(self) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        Return the backend/provider/model used for the most recent execution.

        Returns:
            Tuple of (backend_name, provider_name, model_name).
            Provider name may be None if no provider was used or no provider configured.
        """
        backend, model = self.get_last_backend_and_model()
        provider = self._provider_manager.get_last_used_provider_name(backend) if backend else None

        return backend, provider, model

    # ---------- Compatibility Helpers ----------
    def switch_to_conflict_model(self) -> None:
        try:
            cli = self._get_or_create_client(self._current_backend_name())
            if hasattr(cli, "switch_to_conflict_model") and callable(getattr(cli, "switch_to_conflict_model")):
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
        return cli.check_mcp_server_configured(server_name)  # type: ignore[no-any-return]

    def add_mcp_server_config(self, server_name: str, command: str, args: list[str]) -> bool:
        """Add MCP server configuration for the current backend.

        Args:
            server_name: Name of the MCP server (e.g., 'graphrag', 'mcp-pdb')
            command: Command to run the MCP server (e.g., 'uv', '/path/to/script.sh')
            args: Arguments for the command (e.g., ['run', 'main.py'] or [])

        Returns:
            True if configuration was added successfully, False otherwise
        """
        cli = self._get_or_create_client(self._current_backend_name())
        return cli.add_mcp_server_config(server_name, command, args)  # type: ignore[no-any-return]

    def ensure_mcp_server_configured(self, server_name: str, command: str, args: list[str]) -> bool:
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
                logger.error(f"Error configuring MCP server '{server_name}' for backend '{backend_name}': {e}")
                all_success = False

        return all_success


class LLMBackendManager:
    """
    Singleton manager for general-purpose LLM backend operations.

    This singleton manages the general backend for all LLM operations except
    commit messages (PR processing, test fixes, code generation, etc.).

    Thread-safe singleton implementation ensures only one instance exists
    across all threads in the application.

    Usage Pattern:
    -----------
    1. First call: Provide initialization parameters (default_backend, default_client, factories)
       ```python
       manager = LLMBackendManager.get_llm_instance(
           default_backend="gemini",
           default_client=client,
           factories={"gemini": lambda: client}
       )
       ```

    2. Subsequent calls: Call without parameters to get the same instance
       ```python
       manager = LLMBackendManager.get_llm_instance()
       # Returns the same instance created above
       ```

    3. Using convenience functions (recommended):
       ```python
       from auto_coder.backend_manager import get_llm_backend_manager, run_llm_prompt

       # Using TOML configuration file (new approach)
       from auto_coder.cli_helpers import build_backend_manager_from_config
       manager = build_backend_manager_from_config()
       response = run_llm_prompt("Your prompt here")
       ```

    Important Notes:
    --------------
    - Initialization parameters are required ONLY on the first call
    - The singleton is thread-safe and can be accessed from multiple threads
    - Use force_reinitialize=True to reconfigure with new parameters
    - Call manager.close() during application shutdown for cleanup
    - Configuration can now be read from a TOML file at ~/.auto-coder/llm_config.toml
    """

    _instance: Optional[BackendManager] = None
    _message_instance: Optional[BackendManager] = None
    _init_params: Optional[Dict[str, Any]] = None
    _lock = threading.Lock()

    @classmethod
    def get_llm_instance(
        cls,
        default_backend: Optional[str] = None,
        default_client: Optional[Any] = None,
        factories: Optional[Dict[str, Callable[[], Any]]] = None,
        order: Optional[List[str]] = None,
        force_reinitialize: bool = False,
    ) -> BackendManager:
        """
        Get or create the singleton LLM backend manager instance.

        This class method returns the singleton instance for general-purpose LLM operations.
        On first call, initialization parameters must be provided. Subsequent calls
        can omit parameters to retrieve the existing instance.

        Args:
            default_backend: Name of the default backend
            default_client: Default client instance
            factories: Dictionary of backend name to factory function
            order: Optional list specifying backend order
            force_reinitialize: Force reinitialization with new parameters (default: False)

        Returns:
            BackendManager: The singleton instance for general LLM operations

        Raises:
            RuntimeError: If called without initialization parameters on first call
        """
        # Fast path: check if instance exists and is initialized
        with cls._lock:
            # Check if we need to initialize
            if cls._instance is None or force_reinitialize:
                # Validate initialization parameters
                if default_backend is None or default_client is None or factories is None:
                    if cls._instance is None or force_reinitialize:
                        raise RuntimeError("LLMBackendManager.get_llm_instance() must be called with " "initialization parameters (default_backend, default_client, factories) " "on first use or when force_reinitialize=True")
                else:
                    # If force_reinitialize and instance exists, close it first
                    if force_reinitialize and cls._instance is not None:
                        logger.info(f"Reinitializing LLMBackendManager singleton with default backend: {default_backend}")
                        try:
                            cls._instance.close()
                        except Exception:
                            pass  # Best effort cleanup
                    elif cls._instance is None:
                        # Initialize singleton with parameters
                        logger.info(f"Initializing LLMBackendManager singleton with default backend: {default_backend}")

                    # Create new instance (or reuse if force_reinitialize)
                    if cls._instance is None or force_reinitialize:
                        cls._instance = BackendManager(
                            default_backend=default_backend,
                            default_client=default_client,
                            factories=factories,
                            order=order,
                        )
                        cls._init_params = {
                            "default_backend": default_backend,
                            "default_client": default_client,
                            "factories": factories,
                            "order": order,
                        }
            elif default_backend is not None or default_client is not None or factories is not None:
                # Parameters provided but instance already exists (and not forcing reinit)
                # This is allowed - we just ignore the parameters and return existing instance
                logger.debug("LLMBackendManager singleton already initialized, ignoring new parameters")

            return cls._instance

    @classmethod
    def reset_singleton(cls) -> None:
        """
        Reset the singleton instance.

        This should only be used in tests or when you need to completely
        reinitialize the singleton with new parameters.

        Thread-safe: Uses locks to ensure reset happens atomically.
        """
        with cls._lock:
            if cls._instance is not None:
                logger.info("Resetting LLMBackendManager singleton")
                try:
                    cls._instance.close()
                except Exception:
                    pass  # Best effort cleanup
            cls._instance = None
            cls._init_params = None

    @classmethod
    def is_initialized(cls) -> bool:
        """
        Check if the singleton instance has been initialized.

        Returns:
            bool: True if instance exists, False otherwise
        """
        with cls._lock:
            return cls._instance is not None

    @classmethod
    def get_message_instance(
        cls,
        default_backend: Optional[str] = None,
        default_client: Optional[Any] = None,
        factories: Optional[Dict[str, Callable[[], Any]]] = None,
        order: Optional[List[str]] = None,
        force_reinitialize: bool = False,
    ) -> BackendManager:
        """
        Get or create the singleton backend manager instance for message operations.

        Args:
            default_backend: Name of the default backend
            default_client: Default client instance
            factories: Dictionary of backend name to factory function
            order: Optional list specifying backend order
            force_reinitialize: Force reinitialization with new parameters (default: False)

        Returns:
            BackendManager: The singleton instance for message generation operations

        Raises:
            RuntimeError: If called without initialization parameters on first call
        """
        # Fast path: check if instance exists and is initialized
        with cls._lock:
            # Check if we need to initialize
            if cls._message_instance is None or force_reinitialize:
                # Validate initialization parameters
                if default_backend is None or default_client is None or factories is None:
                    if cls._message_instance is None or force_reinitialize:
                        raise RuntimeError("LLMBackendManager.get_message_instance() must be called with " "initialization parameters (default_backend, default_client, factories) " "on first use or when force_reinitialize=True")
                else:
                    # If force_reinitialize and instance exists, close it first
                    if force_reinitialize and cls._message_instance is not None:
                        logger.info(f"Reinitializing message LLMBackendManager singleton with default backend: {default_backend}")
                        try:
                            cls._message_instance.close()
                        except Exception:
                            pass  # Best effort cleanup
                    elif cls._message_instance is None:
                        # Initialize singleton with parameters
                        logger.info(f"Initializing message LLMBackendManager singleton with default backend: {default_backend}")

                    # Create new instance (or reuse if force_reinitialize)
                    if cls._message_instance is None or force_reinitialize:
                        cls._message_instance = BackendManager(
                            default_backend=default_backend,
                            default_client=default_client,
                            factories=factories,
                            order=order,
                        )
            elif default_backend is not None or default_client is not None or factories is not None:
                # Parameters provided but instance already exists (and not forcing reinit)
                # This is allowed - we just ignore the parameters and return existing instance
                logger.debug("LLMBackendManager message singleton already initialized, ignoring new parameters")

            return cls._message_instance


# Global convenience functions for message backend operations


def get_message_backend_manager(
    default_backend: Optional[str] = None,
    default_client: Optional[Any] = None,
    factories: Optional[Dict[str, Callable[[], Any]]] = None,
    order: Optional[List[str]] = None,
    force_reinitialize: bool = False,
) -> BackendManager:
    """
    Get the global message backend manager singleton instance.

    This is a convenience function that delegates to LLMBackendManager.get_message_instance().
    Use this when you need to access the message backend manager from anywhere in your code.

    Args:
        default_backend: Name of the default backend
        default_client: Default client instance
        factories: Dictionary of backend name to factory function
        order: Optional list specifying backend order
        force_reinitialize: Force reinitialization with new parameters (default: False)

    Returns:
        BackendManager: The singleton instance for message generation operations

    Raises:
        RuntimeError: If called without initialization parameters on first call
    """
    return LLMBackendManager.get_message_instance(
        default_backend=default_backend,
        default_client=default_client,
        factories=factories,
        order=order,
        force_reinitialize=force_reinitialize,
    )


def run_llm_message_prompt(prompt: str) -> str:
    """
    Run a prompt using the global message backend manager.

    This is a convenience function that provides a simple way to execute message generation
    tasks using the global message backend manager singleton.

    Args:
        prompt: The prompt to send to the LLM

    Returns:
        str: The response from the LLM

    Raises:
        RuntimeError: If the message backend manager hasn't been initialized
    """
    manager = LLMBackendManager.get_message_instance()
    if manager is None:
        raise RuntimeError("Message backend manager not initialized. " "Call get_message_backend_manager() with initialization parameters first.")
    return manager._run_llm_cli(prompt)  # type: ignore[no-any-return]


def get_message_backend_and_model() -> Tuple[Optional[str], Optional[str]]:
    """
    Get the backend and model used for the most recent message generation.

    Returns:
        Tuple[Optional[str], Optional[str]]: (backend_name, model_name) or (None, None) if not available
    """
    manager = LLMBackendManager.get_message_instance()
    if manager is None:
        return None, None  # type: ignore[unreachable]
    return manager.get_last_backend_and_model()


# Global convenience functions for general LLM backend operations


def get_llm_backend_manager(
    default_backend: Optional[str] = None,
    default_client: Optional[Any] = None,
    factories: Optional[Dict[str, Callable[[], Any]]] = None,
    order: Optional[List[str]] = None,
    force_reinitialize: bool = False,
) -> BackendManager:
    """
    Get the global LLM backend manager singleton instance.

    This is a convenience function that delegates to LLMBackendManager.get_llm_instance().
    Use this when you need to access the general LLM backend manager from anywhere in your code.

    Args:
        default_backend: Name of the default backend
        default_client: Default client instance
        factories: Dictionary of backend name to factory function
        order: Optional list specifying backend order
        force_reinitialize: Force reinitialization with new parameters (default: False)

    Returns:
        BackendManager: The singleton instance for general LLM operations

    Raises:
        RuntimeError: If called without initialization parameters on first call
    """
    return LLMBackendManager.get_llm_instance(
        default_backend=default_backend,
        default_client=default_client,
        factories=factories,
        order=order,
        force_reinitialize=force_reinitialize,
    )


def run_llm_prompt(prompt: str) -> str:
    """
    Run a prompt using the global LLM backend manager.

    This is a convenience function that provides a simple way to execute general LLM
    tasks using the global LLM backend manager singleton.

    Args:
        prompt: The prompt to send to the LLM

    Returns:
        str: The response from the LLM

    Raises:
        RuntimeError: If the LLM backend manager hasn't been initialized
    """
    manager = LLMBackendManager.get_llm_instance()
    if manager is None:
        raise RuntimeError("LLM backend manager not initialized. " "Call get_llm_backend_manager() with initialization parameters first.")
    return manager._run_llm_cli(prompt)  # type: ignore[no-any-return]


def get_llm_backend_and_model() -> Tuple[Optional[str], Optional[str]]:
    """
    Get the backend and model used for the most recent general LLM execution.

    Returns:
        Tuple[Optional[str], Optional[str]]: (backend_name, model_name) or (None, None) if not available
    """
    manager = LLMBackendManager.get_llm_instance()
    if manager is None:
        return None, None  # type: ignore[unreachable]
    return manager.get_last_backend_and_model()


def get_llm_backend_provider_and_model() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Get the backend, provider, and model used for the most recent general LLM execution.

    Returns:
        Tuple[Optional[str], Optional[str], Optional[str]]: (backend_name, provider_name, model_name)
        or (None, None, None) if not available. Provider name may be None if no provider was used.
    """
    manager = LLMBackendManager.get_llm_instance()
    if manager is None:
        return None, None, None  # type: ignore[unreachable]
    return manager.get_last_backend_provider_and_model()

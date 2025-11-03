"""
BackendManager: 複数バックエンドを循環的に管理し、使用料制限や
apply_workspace_test_fix での同一 current_test_file 3連続時の自動切替を行う。
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from .exceptions import AutoCoderUsageLimitError
from .llm_client_base import LLMBackendManagerBase
from .logger_config import get_logger, log_calls
from .progress_footer import ProgressStage

logger = get_logger(__name__)

# Global singleton instance for general LLM operations
_llm_instance: Optional[BackendManager] = None
_instance_lock = threading.Lock()
_initialization_lock = threading.Lock()


class BackendManager(LLMBackendManagerBase):
    """LLMクライアントを循環的に切替管理するラッパー。

    - _run_llm_cli(prompt) を提供（クライアント互換）
    - run_test_fix_prompt(prompt, current_test_file) は apply_workspace_test_fix 用の拡張:
      同一モデル・同一 current_test_file が3回連続で与えられた場合に次のバックエンドへ循環切替。
      異なる current_test_file が来た場合はデフォルトバックエンドに戻す。
    - 各クライアントが使用料制限に達した場合、AutoCoderUsageLimitError を投げる前提で
      これを受けて次のバックエンドに切替して自動リトライする。
    """

    def __init__(
        self,
        default_backend: str,
        default_client: Any,
        factories: Dict[str, Callable[[], Any]],
        order: Optional[List[str]] = None,
    ) -> None:
        # バックエンド順序（循環）
        self._all_backends = order[:] if order else list(factories.keys())
        # デフォルトを先頭にローテート
        if default_backend in self._all_backends:
            while self._all_backends[0] != default_backend:
                self._all_backends.append(self._all_backends.pop(0))
        else:
            self._all_backends.insert(0, default_backend)
        # クライアントのキャッシュ（遅延生成）
        self._factories = factories
        self._clients: Dict[str, Optional[Any]] = {k: None for k in self._all_backends}
        self._clients[default_backend] = default_client
        self._current_idx = 0
        self._default_backend = default_backend

        # apply_workspace_test_fix のための直近プロンプト/モデル/バックエンド追跡
        self._last_prompt: Optional[str] = None
        self._last_backend: Optional[str] = None
        # 直近で使用したモデル名も記録し、テスト用CSVに正しい情報を残せるようにする
        self._last_model: Optional[str] = getattr(default_client, "model_name", None)
        # current_test_file の追跡（3回同じファイルが続いたら切替）
        self._last_test_file: Optional[str] = None
        self._same_test_file_count: int = 0

    # ---------- 基本操作 ----------
    def _current_backend_name(self) -> str:
        return self._all_backends[self._current_idx]

    def _get_or_create_client(self, name: str) -> Any:
        cli = self._clients.get(name)
        if cli is not None:
            return cli
        # 遅延生成
        fac = self._factories.get(name)
        if fac is None:
            raise RuntimeError(f"No factory for backend: {name}")
        try:
            cli = fac()
            self._clients[name] = cli
            return cli
        except Exception as e:
            # 生成できない場合はスキップ（次へ）
            raise RuntimeError(f"Failed to initialize backend '{name}': {e}")

    def _switch_to_index(self, idx: int) -> None:
        self._current_idx = idx % len(self._all_backends)
        try:
            # モデル切替連動：デフォルト戻し
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
        # デフォルトの位置へ
        try:
            idx = self._all_backends.index(self._default_backend)
        except ValueError:
            idx = 0
        self._switch_to_index(idx)
        logger.info(
            f"BackendManager: switched back to default backend -> {self._current_backend_name()}"
        )

    # ---------- 直接互換メソッド ----------
    @log_calls
    def _run_llm_cli(self, prompt: str) -> str:
        """通常の実行（使用料制限時は循環的リトライ）。"""
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
                    out: str = cli._run_llm_cli(prompt)
                    # 実行に成功した場合のみ、直近利用したバックエンド/モデルを更新する
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
                    # 他の失敗は伝播（使用料制限以外は即エラー）
                    last_error = e
                    break
        if last_error:
            raise last_error
        raise RuntimeError("No backend available to run prompt")

    # ---------- apply_workspace_test_fix 専用 ----------
    @log_calls
    def run_test_fix_prompt(
        self, prompt: str, current_test_file: Optional[str] = None
    ) -> str:
        """apply_workspace_test_fix 用の実行。
        - 同一 current_test_file が3回連続で与えられたら次のバックエンドへ切替
        - 異なる current_test_file が来たらデフォルトに戻す
        - その上で _run_llm_cli を呼ぶ（使用料制限時はさらに循環）
        """
        # 現在のバックエンドとモデル名を取得
        current_backend = self._current_backend_name()

        if self._last_test_file is None or current_test_file != self._last_test_file:
            # test_file が変わった → デフォルトに戻す（今回が1回目）
            self.switch_to_default_backend()
            self._same_test_file_count = 1
        else:
            # 同一 test_file
            if self._last_backend == current_backend:
                # 直前までに同一バックエンドで2回続いていたら、3回目の実行前に切替
                if self._same_test_file_count >= 2:
                    self.switch_to_next_backend()
                    self._same_test_file_count = 1
                else:
                    self._same_test_file_count += 1
            else:
                # バックエンドが変わっている場合はカウンタリセット（今回が1回目）
                self._same_test_file_count = 1

        # 実行
        with ProgressStage(f"Running LLM: {self._current_backend_name()}"):
            out: str = self._run_llm_cli(prompt)

        # 状態更新
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

    # ---------- 互換補助 ----------
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
        """クライアントの close があれば呼ぶ。"""
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
        return cli.add_mcp_server_config(server_name, command, args)  # type: ignore[no-any-return]

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


class LLMBackendManager:
    """
    Singleton manager for general-purpose LLM backend operations.

    This singleton manages the general backend for all LLM operations except
    commit messages (PR processing, test fixes, code generation, etc.).

    Thread-safe singleton implementation ensures only one instance exists
    across all threads in the application.
    """

    _instance: Optional[BackendManager] = None
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
                if (
                    default_backend is None
                    or default_client is None
                    or factories is None
                ):
                    if cls._instance is None or force_reinitialize:
                        raise RuntimeError(
                            "LLMBackendManager.get_llm_instance() must be called with "
                            "initialization parameters (default_backend, default_client, factories) "
                            "on first use or when force_reinitialize=True"
                        )
                else:
                    # If force_reinitialize and instance exists, close it first
                    if force_reinitialize and cls._instance is not None:
                        logger.info(
                            f"Reinitializing LLMBackendManager singleton with default backend: {default_backend}"
                        )
                        try:
                            cls._instance.close()
                        except Exception:
                            pass  # Best effort cleanup
                    elif cls._instance is None:
                        # Initialize singleton with parameters
                        logger.info(
                            f"Initializing LLMBackendManager singleton with default backend: {default_backend}"
                        )

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
            elif (
                default_backend is not None
                or default_client is not None
                or factories is not None
            ):
                # Parameters provided but instance already exists (and not forcing reinit)
                # This is allowed - we just ignore the parameters and return existing instance
                logger.debug(
                    "LLMBackendManager singleton already initialized, ignoring new parameters"
                )

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

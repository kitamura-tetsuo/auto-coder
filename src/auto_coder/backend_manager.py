"""
BackendManager: 複数バックエンドを循環的に管理し、使用料制限や
apply_workspace_test_fix での同一プロンプト3連続時の自動切替を行う。
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Any

from .logger_config import get_logger
from .exceptions import AutoCoderUsageLimitError

logger = get_logger(__name__)


class BackendManager:
    """LLMクライアントを循環的に切替管理するラッパー。

    - _run_llm_cli(prompt) を提供（クライアント互換）
    - run_test_fix_prompt(prompt) は apply_workspace_test_fix 用の拡張:
      同一モデル・同一プロンプトが3回連続で与えられた場合に次のバックエンドへ循環切替。
      異なるプロンプトが来た場合はデフォルトバックエンドに戻す。
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
        self._last_model: Optional[str] = None
        self._last_backend: Optional[str] = None
        self._same_prompt_count: int = 0

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
            if hasattr(cli, 'switch_to_default_model') and callable(getattr(cli, 'switch_to_default_model')):
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
        # デフォルトの位置へ
        try:
            idx = self._all_backends.index(self._default_backend)
        except ValueError:
            idx = 0
        self._switch_to_index(idx)
        logger.info(f"BackendManager: switched back to default backend -> {self._current_backend_name()}")

    # ---------- 直接互換メソッド ----------
    def _run_llm_cli(self, prompt: str) -> str:
        """通常の実行（使用料制限時は循環的リトライ）。"""
        attempts = 0
        tried: set[int] = set()
        last_error: Optional[Exception] = None
        while attempts < len(self._all_backends):
            name = self._current_backend_name()
            if self._current_idx in tried:
                self.switch_to_next_backend()
                attempts += 1
                continue
            tried.add(self._current_idx)
            try:
                cli = self._get_or_create_client(name)
                out = cli._run_llm_cli(prompt)
                return out
            except AutoCoderUsageLimitError as e:
                logger.warning(f"Backend '{name}' hit usage limit: {e}. Rotating to next backend.")
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
    def run_test_fix_prompt(self, prompt: str) -> str:
        """apply_workspace_test_fix 用の実行。
        - 同一モデル・同一プロンプトが3回連続で与えられたら次のバックエンドへ切替
        - 異なるプロンプトが来たらデフォルトに戻す
        - その上で _run_llm_cli を呼ぶ（使用料制限時はさらに循環）
        """
        # 現在のバックエンドとモデル名を取得
        current_backend = self._current_backend_name()
        cur_cli = self._get_or_create_client(current_backend)
        cur_model = getattr(cur_cli, 'model_name', None)

        if self._last_prompt is None or prompt != self._last_prompt:
            # プロンプトが変わった → デフォルトに戻す（今回が1回目）
            self.switch_to_default_backend()
            self._same_prompt_count = 1
        else:
            # 同一プロンプト
            if self._last_backend == current_backend:
                # 直前までに同一バックエンドで2回続いていたら、3回目の実行前に切替
                if self._same_prompt_count >= 2:
                    self.switch_to_next_backend()
                    self._same_prompt_count = 0
                else:
                    self._same_prompt_count += 1
            else:
                # バックエンドが変わっている場合はカウンタリセット（今回が1回目）
                self._same_prompt_count = 1

        # 実行
        out = self._run_llm_cli(prompt)

        # 状態更新
        cur_cli2 = self._get_or_create_client(self._current_backend_name())
        self._last_prompt = prompt
        self._last_model = getattr(cur_cli2, 'model_name', None)
        self._last_backend = self._current_backend_name()
        return out

    # ---------- 互換補助 ----------
    def switch_to_conflict_model(self) -> None:
        try:
            cli = self._get_or_create_client(self._current_backend_name())
            if hasattr(cli, 'switch_to_conflict_model') and callable(getattr(cli, 'switch_to_conflict_model')):
                cli.switch_to_conflict_model()
        except Exception:
            pass

    def switch_to_default_model(self) -> None:
        try:
            cli = self._get_or_create_client(self._current_backend_name())
            if hasattr(cli, 'switch_to_default_model') and callable(getattr(cli, 'switch_to_default_model')):
                cli.switch_to_default_model()
        except Exception:
            pass

    def close(self) -> None:
        """クライアントの close があれば呼ぶ。"""
        for name, cli in list(self._clients.items()):
            try:
                if cli and hasattr(cli, 'close') and callable(getattr(cli, 'close')):
                    cli.close()
            except Exception:
                pass


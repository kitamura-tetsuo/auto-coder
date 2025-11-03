# Global Backend Manager 使用方法

## 概要

グローバルなシングルトンとしてどこからでもLLMバックエンドマネージャーを使用できるようにしました。

## 利用できるグローバル関数

### メインLLM操作用

```python
from auto_coder.backend_manager import get_llm_backend_manager, run_llm_prompt, get_llm_backend_and_model

# LLMバックエンドの取得（初回のみ初期化が必要）
manager = get_llm_backend_manager(
    default_backend="codex",
    default_client=client,
    factories={"codex": lambda: client}
)

# プロンプト実行
response = run_llm_prompt("your prompt here")

# 直近のバックエンド情報取得
backend, model = get_llm_backend_and_model()
```

## LLMBackendManager クラス

### 基本的なシングルトンアクセス

```python
from auto_coder.backend_manager import LLMBackendManager

# LLMバックエンドの取得
manager = LLMBackendManager.get_llm_instance(
    default_backend="gemini",
    default_client=client,
    factories={"gemini": lambda: client}
)
```

## 使用例

### 基本的な使用パターン

```python
# 1. インポート
from auto_coder.backend_manager import (
    LLMBackendManager,
    get_llm_backend_manager,
    run_llm_prompt,
)

# 2. 初期化（一度だけ実行）
manager = LLMBackendManager.get_llm_instance(
    default_backend="gemini",
    default_client=gemini_client,
    factories={"gemini": lambda: gemini_client}
)

# 3. 使用
response = run_llm_prompt("Generate some code")

# 4. バックエンド情報の取得
backend, model = get_llm_backend_and_model()
print(f"Using {backend} with model {model}")
```

### エラーハンドリング

```python
try:
    manager = get_llm_backend_manager(
        default_backend="invalid-backend",
        default_client=None,
        factories={}
    )
except RuntimeError as e:
    print(f"Initialization error: {e}")

# 初回呼び出しではパラメータが必須
manager = get_llm_backend_manager()  # RuntimeError が発生

# 二回目以降はパラメータなしで呼び出し可能
manager = get_llm_backend_manager()  # 既存のインスタンスを返す
```

## スレッドセーフ性

すべての関数はスレッドセーフに設計されており、複数のスレッドから同時にアクセスしても安全です。

## 注意事項

1. **初期化は一度だけ**: 初回呼び出し時にのみ初期化パラメータが必要です
2. **リソース管理**: アプリケーション終了時は適切にクリーンアップしてください
3. **設定変更**: `force_reinitialize=True` で設定を変更できます
4. **後方互換性**: 既存の `LLMBackendManager.get_llm_instance()` も繼續利用可能です
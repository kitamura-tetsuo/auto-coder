# Qwen認証情報の渡し方オプション

## 概要

QwenClientは、OpenAI互換のAPIキーとベースURLを2つの方法で渡すことができます：

1. **環境変数経由（デフォルト）**: `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`OPENAI_MODEL`を環境変数として設定
2. **コマンドラインオプション経由**: `--api-key`、`--base-url`、`-m`をqwen CLIに直接渡す

## CLIオプション

### `--qwen-use-env-vars` / `--qwen-use-cli-options`

認証情報の渡し方を選択します。

- `--qwen-use-env-vars`（デフォルト）: 環境変数経由で認証情報を渡す
- `--qwen-use-cli-options`: コマンドラインオプション経由で認証情報を渡す

**使用例:**

```bash
# 環境変数経由（デフォルト）
auto-coder process-issues --repo owner/repo --backend qwen \
  --openai-api-key sk-xxx --openai-base-url https://api.example.com

# コマンドラインオプション経由
auto-coder process-issues --repo owner/repo --backend qwen \
  --openai-api-key sk-xxx --openai-base-url https://api.example.com \
  --qwen-use-cli-options
```

### `--qwen-preserve-env` / `--qwen-clear-env`

既存の`OPENAI_*`環境変数を保持するか、クリアするかを選択します。

- `--qwen-clear-env`（デフォルト）: 既存の環境変数をクリアしてから新しい値を設定
- `--qwen-preserve-env`: 既存の環境変数を保持し、新しい値を追加

**使用例:**

```bash
# 既存の環境変数をクリア（デフォルト）
auto-coder process-issues --repo owner/repo --backend qwen \
  --openai-api-key sk-xxx

# 既存の環境変数を保持
auto-coder process-issues --repo owner/repo --backend qwen \
  --openai-api-key sk-xxx --qwen-preserve-env
```

## プログラムからの使用

### QwenClientの初期化

```python
from auto_coder.qwen_client import QwenClient

# デフォルト（環境変数経由、既存の環境変数をクリア）
client = QwenClient(
    model_name="qwen3-coder-plus",
    openai_api_key="sk-xxx",
    openai_base_url="https://api.example.com"
)

# コマンドラインオプション経由
client = QwenClient(
    model_name="qwen3-coder-plus",
    openai_api_key="sk-xxx",
    openai_base_url="https://api.example.com",
    use_env_vars=False  # CLIオプションを使用
)

# 既存の環境変数を保持
client = QwenClient(
    model_name="qwen3-coder-plus",
    openai_api_key="sk-xxx",
    openai_base_url="https://api.example.com",
    preserve_existing_env=True  # 既存の環境変数を保持
)
```

## 挙動の違い

### 環境変数経由（デフォルト）

```bash
# 実行されるコマンド
OPENAI_API_KEY=sk-xxx OPENAI_BASE_URL=https://api.example.com OPENAI_MODEL=qwen3-coder-plus \
  qwen -y -m qwen3-coder-plus -p "prompt text"
```

### コマンドラインオプション経由

```bash
# 実行されるコマンド
qwen -y --api-key sk-xxx --base-url https://api.example.com -m qwen3-coder-plus -p "prompt text"
```

## トラブルシューティング

### 環境変数が正しく渡されない場合

環境変数経由で認証情報が正しく渡されない場合は、コマンドラインオプション経由を試してください：

```bash
auto-coder process-issues --repo owner/repo --backend qwen \
  --openai-api-key sk-xxx --openai-base-url https://api.example.com \
  --qwen-use-cli-options
```

### 既存の環境変数と競合する場合

既存の`OPENAI_*`環境変数と競合する場合は、`--qwen-clear-env`（デフォルト）を使用して、既存の環境変数をクリアしてから新しい値を設定してください。

## 関連ファイル

- `src/auto_coder/qwen_client.py`: QwenClientの実装
- `src/auto_coder/cli_commands_main.py`: CLIコマンドの実装
- `src/auto_coder/cli_helpers.py`: バックエンドマネージャーの構築
- `tests/test_qwen_client_cli_options.py`: 新しいオプションのテスト


# OpenRouter設定ガイド

## 概要
Codex CLIでOpenRouter経由でQwen3 Coderを使用するための設定が完了しました。

## 設定内容

### 1. 環境変数の設定
以下の環境変数が`~/.bashrc`に追加されました：

```bash
export OPENAI_API_KEY="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"
export OPENAI_BASE_URL="https://openrouter.ai/api/v1"
export OPENAI_MODEL="qwen/qwen3-coder:free"
```

### 2. Codex設定ファイル（~/.codex/config.toml）
以下の設定が追加されました：

```toml
model = "qwen/qwen3-coder:free"
model_provider = "openrouter"

# OpenRouter configuration
[model_providers.openrouter]
name = "OpenRouter"
base_url = "https://openrouter.ai/api/v1"
env_key = "OPENAI_API_KEY"
```

## 使用方法

### 現在のセッションで環境変数を有効化
```bash
source ~/.bashrc
```

または

```bash
export OPENAI_API_KEY="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"
export OPENAI_BASE_URL="https://openrouter.ai/api/v1"
export OPENAI_MODEL="qwen/qwen3-coder:free"
```

### Codex CLIの実行
設定が完了したので、通常通りCodexを使用できます：

```bash
codex "Hello, how are you?"
```

または

```bash
codex exec "Write a Python function to calculate fibonacci numbers"
```

## 設定の確認

### 環境変数の確認
```bash
env | grep OPENAI
```

### Codex設定の確認
```bash
cat ~/.codex/config.toml
```

### Codexバージョンの確認
```bash
codex --version
```

## バックアップ
元の設定ファイルは以下にバックアップされています：
- `~/.codex/config.toml.backup`

## トラブルシューティング

### 設定が反映されない場合
1. 新しいターミナルセッションを開く
2. または `source ~/.bashrc` を実行

### モデルプロバイダーを変更したい場合
`~/.codex/config.toml`の以下の行を編集：
```toml
model_provider = "openrouter"  # 他のプロバイダーに変更可能
```

### 別のモデルを使用したい場合
`~/.codex/config.toml`の以下の行を編集：
```toml
model = "qwen/qwen3-coder:free"  # 他のOpenRouterモデルに変更可能
```

## 参考リンク
- [Codex CLI Configuration Documentation](https://developers.openai.com/codex/local-config/)
- [OpenRouter Models](https://openrouter.ai/models)
- [Codex GitHub Repository](https://github.com/openai/codex)


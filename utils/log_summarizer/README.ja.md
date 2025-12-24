# ログ要約 反復的改善ツール (Iterative Refinement Tool)

このツールは、LLMによって生成された「ゴールドスタンダード（正解データ）」と比較することで、決定論的なエラーログ要約アルゴリズムを反復的に改善するために設計されています。
複数の候補アルゴリズムを並行して維持し、異なるアプローチを実験することができます。

## 構成

- `main.py`: CLIのエントリーポイントです。
- `llm_wrapper.py`: `auto-coder` の LLM クライアントをラップして要約を生成します。
- `candidate.py`: 候補アルゴリズム（改善対象のロジック）と `ALGORITHM_REGISTRY` が含まれています。

## セットアップ

プロジェクトのルートディレクトリに移動し、仮想環境を使用してください。

```bash
# 例
source .venv/bin/activate
```

## 使い方

### 1. ゴールドスタンダード要約の生成 (LLM使用)

これには `auto-coder` の LLM 設定が必要です。

```bash
python utils/log_summarizer/main.py generate-gold \
  --log-file /path/to/your/log_dir/ \
  --output gold_summary.json \
  --backend gemini  # オプション: LLMバックエンドを指定
```

### 2. 候補アルゴリズムの実行

特定のアルゴリズムを指定して実行できます。デフォルトは `baseline` です。

```bash
python utils/log_summarizer/main.py run-candidate \
  --log-file /path/to/your/log_dir/ \
  --output candidate_summary.json \
  --algorithm baseline
```

### 3. 全アルゴリズムの実行

登録されているすべてのアルゴリズムを一度に実行します。

```bash
python utils/log_summarizer/main.py run-all \
  --log-file /path/to/your/log_dir/ \
  --output-dir /path/to/output/dir
```
出力ファイル名は `{log_filename}_{algorithm}.json` の形式になります。

### 4. 評価 (Evaluate)

候補要約 (JSON) をゴールドスタンダード (JSON) と比較します。
スコアは行ごとの完全一致（空白は無視）に基づいています。

```bash
python src/auto-coder/utils/log_summarizer/main.py evaluate \
  --gold-file gold_summary.json \
  --candidate-file candidate_summary.json
```

### 5. 全て評価 (Evaluate All - Ranking)

ディレクトリ内のすべての JSON ファイルをゴールドスタンダードと比較し、F1スコアでランク付けします。

```bash
python src/auto-coder/utils/log_summarizer/main.py evaluate-all \
  --gold-file gold_summary.json \
  --candidates-dir /path/to/candidates/
```

## 新しいアルゴリズムの追加

1.  `src/auto-coder/utils/log_summarizer/candidate.py` を開きます。
2.  `BaseSummarizer` を継承した新しいクラスを定義します。
3.  `summarize` メソッドを実装します。
4.  `ALGORITHM_REGISTRY` に登録します。

```python
class MyNewAlgo(BaseSummarizer):
    def summarize(self, log_content: str) -> str:
        # ロジックを実装
        return "Summary..."

ALGORITHM_REGISTRY = {
    "baseline": BaselineSummarizer,
    "my_algo": MyNewAlgo,
}
```

## 反復的改善のワークフロー

1.  対象のログに対してゴールドスタンダード要約を生成します。
2.  候補アルゴリズムを実行します。
3.  出力を比較します。
4.  `candidate.py` のコードを修正します。
5.  再実行して改善を確認します。

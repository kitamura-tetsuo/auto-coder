# Progress Decorators for Auto-Coder

このドキュメントは、`ProgressStage()`コンテキストマネージャーと同等の機能を提供するデコレーターについて説明します。

## 概要

`src/auto_coder/progress_decorators.py` には、メソッドの実行中に自動でプログレス管理を行うデコレーターが実装されています。これらは `ProgressStage` コンテキストマネージャーと同じ機能を提供しますが、デコレーターとして使用できます。

## デコレーターの種類

### 1. `@progress_stage`

最も柔軟なデコレーターで、ProgressStageコンテキストマネージャーと同等の引数を受け付けます。

#### 使用例

```python
from src.auto_coder.progress_decorators import progress_stage

class MyProcessor:
    @progress_stage()
    def process_simple(self):
        # 自動生成されたステージ名（"Process Simple"）を使用
        pass
    
    @progress_stage("Custom Stage Name")
    def process_with_custom_name(self):
        # カスタムステージ名
        pass
    
    @progress_stage("PR", 123, "Analyzing")
    def analyze_pr(self):
        # PRコンテキスト付き
        pass
    
    @progress_stage("Issue", 456, "Processing", [789], "feature-branch")
    def process_issue_with_context(self):
        # フルコンテキスト情報付き
        pass
```

#### 引数の仕様

- `()`: メソッド名を自動生成（`method_name → Method Name`）
- `("stage_name")`: カスタムステージ名
- `("item_type", item_number, "stage_name")`: アイテム情報とステージ
- `("item_type", item_number, "stage_name", related_issues, branch_name)`: フルコンテキスト

### 2. `@progress_method`

 よりシンプルな構文を提供する代替デコレーターです。

```python
from src.auto_coder.progress_decorators import progress_method

class MyProcessor:
    @progress_method()
    def my_method(self):
        # メソッド名をステージ名として使用
        pass
    
    @progress_method("Custom Stage")
    def another_method(self):
        # カスタムステージ名
        pass
```

### 3. `@progress_context_item`

アイテムコンテキストを設定し、自動的にクリアするデコレーターです。

```python
from src.auto_coder.progress_decorators import progress_context_item

class MyProcessor:
    @progress_context_item("PR", 123, "Analyzing")
    def analyze_pr(self):
        # PRコンテキスト設定、実行後に自動クリア
        pass
```

### 4. `ProgressStageDecorator` クラス

クラスベースデコレーターで、より複雑なシナリオに対応できます。

```python
from src.auto_coder.progress_decorators import ProgressStageDecorator

class MyProcessor:
    @ProgressStageDecorator("Processing")
    def process_item(self):
        pass
        
    @ProgressStageDecorator("PR", 123, "Analyzing")
    def analyze_pr(self):
        pass
```

## ProgressStage との比較

### ProgressStage (コンテキストマネージャー)
```python
from src.auto_coder.progress_footer import ProgressStage

def my_function():
    with ProgressStage("PR", 123, "Analyzing"):
        # 処理実行
        pass
```

### progress_stage デコレーター
```python
from src.auto_coder.progress_decorators import progress_stage

class MyProcessor:
    @progress_stage("PR", 123, "Analyzing")
    def my_method(self):
        # 処理実行（自動的にプログレス管理）
        pass
```

## ネストされたデコレーター

デコレーターは正常にネストでき、内側のデコレーターが外側のデコレーターのステージスタックに追加されます。

```python
class MyProcessor:
    @progress_stage("Outer Operation")
    def outer_operation(self):
        # 外側ステージ: [PR #123] Outer Operation
        
        self.analyze_pr()  # 内側ステージ: [PR #123] Outer Operation / Analyzing
        self.process_simple()  # 内側ステージ: [PR #123] Outer Operation / Process Simple
        
        # 外側ステージに復帰: [PR #123] Outer Operation
```

## 実用的な使用例

### GitHub Issue プロセッサーでの使用

```python
from src.auto_coder.progress_decorators import progress_stage

class IssueProcessor:
    @progress_stage("Issue", 1, "Analyzing")
    def analyze_issue(self, issue):
        # Issueの分析処理
        self.validate_issue(issue)
        self.create_branch(issue)
    
    @progress_stage("Issue", 1, "Implementing")
    def implement_fix(self, issue):
        # 修正実装
        self.modify_code(issue)
        self.run_tests()
    
    @progress_stage("Issue", 1, "Creating PR")
    def create_pull_request(self, issue):
        # PR作成
        self.commit_changes()
        self.push_branch()
        self.create_pr()
```

### PR プロセッサーでの使用

```python
from src.auto_coder.progress_decorators import progress_stage, progress_context_item

class PRProcessor:
    @progress_stage("PR", 123, "Validating")
    def validate_pr(self, pr):
        # PRの検証
        self.check_tests(pr)
        self.check_conflicts(pr)
    
    @progress_context_item("PR", 123, "Merging")
    def merge_pr(self, pr):
        # PRのマージ（完了後に自動クリア）
        self.merge_branch(pr)
        self.close_issue(pr)
```

## エラーハンドリング

デコレーターは自動的に `try/finally` ブロックで包装されているため、メthylodが例外を発生させた場合でも、プログレスステージは適切にクリアされます。

```python
@progress_stage("PR", 123, "Analyzing")
def risky_method(self):
    # 例外が発生しても、プログレスステージは適切にポップされる
    raise ValueError("何か問題が発生しました")
```

## まとめ

これらのデコレーターは、ProgressStageコンテキストマネージャーと同等の機能を提供し、メthylodデコレーターとしてより簡潔に使用できます。複雑なネストされた操作や自動的なエラーハンドリングも適切に処理されます。
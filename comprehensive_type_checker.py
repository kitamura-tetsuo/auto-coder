"""
実行前の包括的な型チェック цель: Pythonで他の型ありの言語みたいに網羅的に型エラーを検出する

このファイルは、実行前に型エラーを検出するための包括的な方法を実演します。
"""

import ast
import inspect
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Type, Union

# =============================================================================
# 1. 型安全性向上のためのPython設定
# =============================================================================


class ComprehensiveTypeChecker:
    """実行前の包括的型チェックシステム"""

    def __init__(self):
        self.errors = []
        self.warnings = []

    def run_all_type_checkers(self, target_file: str) -> Dict[str, Any]:
        """複数の型チェックツールを順次実行"""
        results = {
            "mypy": self._run_mypy(target_file),
            "pyright": self._run_pyright(target_file),
            "pylint": self._run_pylint(target_file),
            "custom_checks": self._run_custom_checks(target_file),
        }
        return results

    def _run_mypy(self, target_file: str) -> Dict[str, Any]:
        """Mypyで静的型チェック"""
        try:
            cmd = [
                sys.executable,
                "-m",
                "mypy",
                target_file,
                "--show-error-codes",
                "--pretty",
                "--no-error-summary",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            return {
                "success": result.returncode == 0,
                "output": result.stdout,
                "stderr": result.stderr,
                "errors_count": len(
                    [line for line in result.stdout.split("\n") if "error:" in line]
                ),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _run_pyright(self, target_file: str) -> Dict[str, Any]:
        """Pyrightで厳格型チェック"""
        try:
            # pyproject.tomlから設定を読み取り
            config = self._load_pyright_config()

            cmd = ["npx", "pyright", target_file, "--outputjson"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            if result.stdout:
                try:
                    json_output = json.loads(result.stdout)
                    return {
                        "success": result.returncode == 0,
                        "data": json_output,
                        "raw_output": result.stdout,
                    }
                except json.JSONDecodeError:
                    return {
                        "success": False,
                        "error": "Invalid JSON output",
                        "raw": result.stdout,
                    }
            else:
                return {"success": False, "error": result.stderr}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _run_pylint(self, target_file: str) -> Dict[str, Any]:
        """Pylintで静的解析"""
        try:
            cmd = [sys.executable, "-m", "pylint", target_file, "--output-format=json"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            return {
                "success": result.returncode == 0,
                "output": result.stdout,
                "stderr": result.stderr,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _load_pyright_config(self) -> Dict[str, Any]:
        """pyproject.tomlからPyright設定をロード"""
        config_path = Path("pyproject.toml")
        if config_path.exists():
            # 実際の設定パースはpyprojectライブラリを使用すべきですが、
            # ここでは簡易実装
            return {"strict": True}
        return {}

    def _run_custom_checks(self, target_file: str) -> Dict[str, Any]:
        """カスタム型チェック（AST解析）"""
        try:
            with open(target_file, "r", encoding="utf-8") as f:
                content = f.read()

            tree = ast.parse(content)
            custom_errors = self._analyze_ast_for_type_issues(tree, target_file)

            return {"success": True, "custom_errors": custom_errors}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _analyze_ast_for_type_issues(
        self, tree: ast.AST, filename: str
    ) -> List[Dict[str, Any]]:
        """ASTを解析して既知の型問題を検出"""
        errors = []

        class TypeIssueVisitor(ast.NodeVisitor):
            def visit_Call(self, node):
                # 辞書型のオブジェクトで.get()メソッドを呼び出している箇所を検出
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and hasattr(node, "lineno")
                ):

                    # 簡単なheuristic: 変数名が'checks'で、'success'が最初の引数の場合は要注意
                    # 実際の実装では、より詳細な型追跡が必要
                    errors.append(
                        {
                            "type": "potential_dict_access_on_dataclass",
                            "file": filename,
                            "line": node.lineno,
                            "column": getattr(node, "col_offset", 0),
                            "message": "Potential use of .get() on object that might be a dataclass",
                        }
                    )

                self.generic_visit(node)

        visitor = TypeIssueVisitor()
        visitor.visit(tree)
        return errors


# =============================================================================
# 2. 実行時の型検証デコレータ
# =============================================================================


def runtime_type_check(func):
    """実行時型チェックデコレータ"""

    def wrapper(*args, **kwargs):
        # 関数の型注釈を取得
        sig = inspect.signature(func)
        type_hints = sig.return_annotation

        # 実行前の型チェック
        for i, (param_name, param_value) in enumerate(
            zip(sig.parameters.values(), args)
        ):
            if param_name.annotation != param_name.empty:
                expected_type = param_name.annotation
                if not isinstance(param_value, expected_type):
                    print(
                        f"型エラー in {func.__name__}: パラメータ '{param_name}' は {expected_type} であるべきですが、{type(param_value)} を受け取りました"
                    )

        result = func(*args, **kwargs)

        # 戻り値チェック
        if hasattr(type_hints, "__args__") and type_hints is not None:
            if not isinstance(result, type_hints):
                print(
                    f"型エラー in {func.__name__}: 戻り値は {type_hints} であるべきですが、{type(result)} を返しました"
                )

        return result

    return wrapper


# =============================================================================
# 3. 具体的問題の解決例
# =============================================================================


@dataclass
class GitHubActionsStatusResult:
    """GitHub Actions チェック結果"""

    success: bool = True
    ids: List[int] = field(default_factory=list)


# =============================================================================
# 4. CI/CD用の包括的型チェックスクリプト
# =============================================================================


def main():
    """メイン実行関数"""
    target_file = "src/auto_coder/automation_engine.py"

    checker = ComprehensiveTypeChecker()
    results = checker.run_all_type_checkers(target_file)

    print("=== 包括的型チェック結果 ===")
    total_errors = 0

    for tool_name, result in results.items():
        print(f"\n--- {tool_name.upper()} ---")
        if result["success"]:
            print(f"✅ 成功")
            if "errors_count" in result:
                total_errors += result["errors_count"]
                print(f"エラー数: {result['errors_count']}")
        else:
            print(f"❌ 失敗: {result.get('error', 'Unknown error')}")

        if "output" in result and result["output"]:
            print(f"出力: {result['output'][:500]}...")

    print(f"\n総エラー数: {total_errors}")
    return total_errors == 0


if __name__ == "__main__":
    main()

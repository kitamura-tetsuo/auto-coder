"""
Pytest configuration and fixtures for Auto-Coder tests.
"""

import os
from unittest.mock import Mock

import pytest

from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.gemini_client import GeminiClient
from src.auto_coder.github_client import GitHubClient


# テストの安定化: 外部環境変数とユーザホームの影響を排除（CLIの挙動を一定にするため）
@pytest.fixture(autouse=True)
def _clear_sensitive_env(monkeypatch, request):
    import tempfile

    # Skip HOME mocking for tests that need real HOME directory
    if "_use_real_home" in request.fixturenames:
        # Only clear sensitive env vars, keep real HOME
        for key in ("GITHUB_TOKEN", "GEMINI_API_KEY"):
            monkeypatch.delenv(key, raising=False)
        return

    # 影響のある環境変数をクリア
    for key in ("GITHUB_TOKEN", "GEMINI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    # ホームディレクトリを一時ディレクトリに切り替え、~/.config/gh/hosts.yml 等の実ファイル影響を遮断
    tmp_home = tempfile.mkdtemp(prefix="ac_test_home_")
    monkeypatch.setenv("HOME", tmp_home)


@pytest.fixture
def _use_real_home():
    """Marker fixture to indicate that a test needs the real HOME directory.

    Tests using this fixture will not have their HOME directory mocked.
    This is useful for integration tests that need to access real configuration files.
    """
    pass


@pytest.fixture
def _use_real_commands():
    """Marker fixture to indicate that a test needs real command execution.

    Tests using this fixture will not have git/gh/uv commands mocked.
    This is useful for integration tests that need to run actual commands.
    """
    pass


@pytest.fixture
def mock_github_token():
    """Mock GitHub token for testing."""
    return "test_github_token"


@pytest.fixture
def mock_gemini_api_key():
    """Mock Gemini API key for testing."""
    return "test_gemini_api_key"


@pytest.fixture
def mock_github_client(mock_github_token):
    """Mock GitHub client for testing."""
    client = Mock(spec=GitHubClient)
    client.token = mock_github_token
    return client


@pytest.fixture
def mock_gemini_client(mock_gemini_api_key):
    """Mock Gemini client for testing."""
    client = Mock(spec=GeminiClient)
    client.api_key = mock_gemini_api_key
    client.model_name = "gemini-2.5-pro"
    return client


@pytest.fixture
def mock_automation_engine(mock_github_client, mock_gemini_client):
    """Mock automation engine for testing."""
    engine = Mock(spec=AutomationEngine)
    engine.github = mock_github_client
    engine.gemini = mock_gemini_client
    engine.dry_run = True
    return engine


@pytest.fixture
def sample_issue_data():
    """Sample issue data for testing."""
    return {
        "number": 123,
        "title": "Test Issue",
        "body": "This is a test issue description",
        "state": "open",
        "labels": ["bug", "high-priority"],
        "assignees": ["testuser"],
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
        "url": "https://github.com/test/repo/issues/123",
        "author": "testuser",
        "comments_count": 2,
    }


@pytest.fixture
def sample_pr_data():
    """Sample PR data for testing."""
    return {
        "number": 456,
        "title": "Test Pull Request",
        "body": "This is a test pull request description",
        "state": "open",
        "labels": ["feature"],
        "assignees": ["testuser"],
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
        "url": "https://github.com/test/repo/pull/456",
        "author": "testuser",
        "head_branch": "feature-branch",
        "base_branch": "main",
        "mergeable": True,
        "draft": False,
        "comments_count": 1,
        "review_comments_count": 0,
        "commits_count": 3,
        "additions": 50,
        "deletions": 10,
        "changed_files": 2,
    }


@pytest.fixture
def sample_analysis_result():
    """Sample analysis result for testing."""
    return {
        "category": "bug",
        "priority": "high",
        "complexity": "moderate",
        "estimated_effort": "days",
        "tags": ["backend", "api"],
        "recommendations": [
            {
                "action": "Fix the API endpoint",
                "rationale": "The endpoint is returning incorrect data",
            }
        ],
        "related_components": ["api", "database"],
        "summary": "API endpoint returning incorrect data",
    }


@pytest.fixture
def sample_feature_suggestion():
    """Sample feature suggestion for testing."""
    return {
        "title": "Add user authentication",
        "description": "Implement user authentication system with JWT tokens",
        "rationale": "Users need to be able to securely access their data",
        "priority": "high",
        "complexity": "complex",
        "estimated_effort": "weeks",
        "labels": ["enhancement", "security"],
        "acceptance_criteria": [
            "Users can register with email and password",
            "Users can login and receive JWT token",
            "Protected routes require valid JWT token",
        ],
    }


@pytest.fixture
def test_repo_name():
    """Test repository name."""
    return "test-owner/test-repo"


@pytest.fixture
def temp_reports_dir(tmp_path):
    """Temporary directory for test reports."""
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    return str(reports_dir)


# 実際の git/gh コマンドをテスト中に実行しないようにスタブする
@pytest.fixture(autouse=True)
def stub_git_and_gh_commands(monkeypatch, request):
    import subprocess
    import types

    # Skip command stubbing for tests that need real commands
    if "_use_real_commands" in request.fixturenames:
        return

    orig_run = subprocess.run
    orig_popen = subprocess.Popen

    def _as_text_or_bytes(text_output: str, text: bool):
        if text:
            return text_output, ""
        # bytes 出力
        return text_output.encode("utf-8"), b""

    def fake_run(
        cmd,
        capture_output=False,
        text=False,
        timeout=None,
        cwd=None,
        check=False,
        input=None,
        env=None,
    ):
        try:
            program = None
            if isinstance(cmd, (list, tuple)) and cmd:
                program = cmd[0]
            elif isinstance(cmd, str):
                program = cmd.split()[0]

            if program not in ("git", "gh", "gemini", "codex", "uv"):
                return orig_run(
                    cmd,
                    capture_output=capture_output,
                    text=text,
                    timeout=timeout,
                    cwd=cwd,
                    check=check,
                    input=input,
                    env=env,
                )

            # デフォルトの成功レスポンス
            out_text = ""

            if program == "git":
                # 代表的な呼び出しに対する最小限の正常系出力
                if (
                    isinstance(cmd, (list, tuple))
                    and "status" in cmd
                    and "--porcelain" in cmd
                ):
                    out_text = ""  # 変更なし
                elif isinstance(cmd, (list, tuple)) and "rev-parse" in cmd:
                    out_text = "main"
                elif isinstance(cmd, (list, tuple)) and "merge-base" in cmd:
                    out_text = "abc123"
                else:
                    out_text = ""
            elif program == "gh":
                if (
                    isinstance(cmd, (list, tuple))
                    and len(cmd) >= 3
                    and cmd[1] == "auth"
                    and cmd[2] == "status"
                ):
                    # 認証なしをシミュレーション（トークン未設定のテストを通すため）
                    return types.SimpleNamespace(
                        stdout="", stderr="not logged in", returncode=1
                    )
                if (
                    isinstance(cmd, (list, tuple))
                    and len(cmd) >= 3
                    and cmd[1] == "pr"
                    and cmd[2] == "checks"
                ):
                    # タブ区切り形式の一例（PASS）
                    out_text = "CI / build\tPASS\t1m\thttps://example/check\n"
                elif (
                    isinstance(cmd, (list, tuple))
                    and len(cmd) >= 3
                    and cmd[1] == "run"
                    and cmd[2] == "list"
                ):
                    out_text = "[]"
                elif (
                    isinstance(cmd, (list, tuple))
                    and len(cmd) >= 3
                    and cmd[1] == "run"
                    and cmd[2] == "view"
                    and "--json" in cmd
                ):
                    out_text = '{"jobs":[]}'
                elif (
                    isinstance(cmd, (list, tuple)) and len(cmd) >= 2 and cmd[1] == "api"
                ):
                    # zip ログ取得など。text=False の呼び出しにも対応
                    pass  # 出力は下で text フラグに応じて生成
                else:
                    out_text = ""
            else:  # gemini/codex/uv
                # --version チェックや exec をダミー成功
                out_text = ""

            if (
                isinstance(cmd, (list, tuple))
                and len(cmd) >= 2
                and cmd[0] == "gh"
                and cmd[1] == "api"
            ):
                # API 呼び出しはバイナリ or テキスト空出力でOK
                if text:
                    stdout, stderr = _as_text_or_bytes("", True)
                else:
                    stdout, stderr = _as_text_or_bytes("", False)
            else:
                if text:
                    stdout, stderr = _as_text_or_bytes(out_text, True)
                else:
                    stdout, stderr = _as_text_or_bytes(out_text, False)

            return types.SimpleNamespace(stdout=stdout, stderr=stderr, returncode=0)
        except Exception:
            # 想定外は元の run にフォールバック
            return orig_run(
                cmd,
                capture_output=capture_output,
                text=text,
                timeout=timeout,
                cwd=cwd,
                check=check,
                input=input,
                env=env,
            )

    def fake_popen(
        cmd,
        stdin=None,
        stdout=None,
        stderr=None,
        text=False,
        bufsize=1,
        universal_newlines=None,
        cwd=None,
        env=None,
    ):
        try:
            program = cmd[0] if isinstance(cmd, (list, tuple)) and cmd else None
            if program in ("git", "gh", "gemini", "codex", "uv"):

                class DummyPopen:
                    def __init__(self):
                        # stdout をイテレータにして、逐次読み取りを安全に終了
                        self._lines = [""]
                        self.stdout = iter(self._lines)
                        self.stderr = iter([""])
                        self.pid = 0

                    def wait(self):
                        return 0

                    def poll(self):
                        return 0

                return DummyPopen()
            # universal_newlines は Python3.12 で text と同義。両方指定の齟齬を避ける
            if universal_newlines is not None and text is None:
                text = bool(universal_newlines)
            return orig_popen(
                cmd,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                text=text,
                bufsize=bufsize,
                cwd=cwd,
                env=env,
            )
        except Exception:
            return orig_popen(
                cmd,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                text=text,
                bufsize=bufsize,
                cwd=cwd,
                env=env,
            )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)


# GraphRAG Session Management Test Fixtures
@pytest.fixture
def isolated_graphrag_session():
    """Create isolated session for testing."""
    from pathlib import Path
    from src.auto_coder.graphrag_mcp_integration import GraphRAGMCPIntegration

    graphrag_integration = GraphRAGMCPIntegration()
    session_id = graphrag_integration.create_session(str(Path.cwd().resolve()))
    yield session_id
    # Cleanup handled by integration test teardown


@pytest.fixture
def compatibility_graphrag_setup():
    """Setup for backward compatibility testing."""
    from src.auto_coder.graphrag_mcp_integration import (
        GraphRAGMCPIntegration,
        BackwardCompatibilityLayer,
    )

    # Setup existing behavior for compatibility tests
    graphrag_integration = GraphRAGMCPIntegration()
    compat_layer = BackwardCompatibilityLayer(graphrag_integration)
    return compat_layer


@pytest.fixture
def mock_code_tool():
    """Mock CodeAnalysisTool for testing."""
    from unittest.mock import Mock
    from graphrag_mcp.code_analysis_tool import CodeAnalysisTool

    mock_tool = Mock(spec=CodeAnalysisTool)
    mock_tool.find_symbol = Mock(return_value={"symbol": {"id": "test"}})
    mock_tool.get_call_graph = Mock(return_value={"nodes": [], "edges": []})
    mock_tool.get_dependencies = Mock(return_value={"imports": [], "imported_by": []})
    mock_tool.impact_analysis = Mock(
        return_value={"affected_symbols": [], "affected_files": []}
    )
    mock_tool.semantic_code_search = Mock(return_value={"symbols": []})
    mock_tool.semantic_code_search_in_collection = Mock(return_value={"symbols": []})
    return mock_tool

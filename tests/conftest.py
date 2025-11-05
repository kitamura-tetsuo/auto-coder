"""
Pytest configuration and fixtures for Auto-Coder tests.
"""

import sys
from pathlib import Path

# Ensure 'src' directory is on sys.path so 'auto_coder' package is importable everywhere
_repo_root = Path(__file__).resolve().parents[1]
_src_path = _repo_root / "src"
if str(_src_path) not in sys.path:
    sys.path.insert(0, str(_src_path))

import os
from unittest.mock import Mock

import pytest

from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.backend_manager import LLMBackendManager, get_llm_backend_manager
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

    print(
        f"DEBUG: stub_git_and_gh_commands initialized, orig_run = {orig_run}",
        file=__import__("sys").stderr,
    )

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

            # Commands that should pass through to real subprocess with modified env
            # This ensures PYTHONPATH is set correctly for source-based imports
            pass_through_programs = ("python", "python3", "/usr/bin/python3")
            if program in pass_through_programs:
                # Debug output
                print(
                    f"DEBUG: Intercepted python command: {program}",
                    file=__import__("sys").stderr,
                )

                # Ensure PYTHONPATH includes user site-packages and current directory
                import os
                import site

                # Get user site-packages path
                user_site = site.getusersitepackages()
                print(f"DEBUG: user_site = {user_site!r}", file=__import__("sys").stderr)
                print(f"DEBUG: cwd = {os.getcwd()!r}", file=__import__("sys").stderr)

                # Build environment with proper PYTHONPATH
                if env is None:
                    env = os.environ.copy()
                else:
                    env = env.copy()

                # Add user site-packages and current directory to PYTHONPATH
                pythonpath = env.get("PYTHONPATH", "")
                print(
                    f"DEBUG: initial pythonpath = {pythonpath!r}",
                    file=__import__("sys").stderr,
                )
                paths_to_add = [
                    user_site,
                    os.getcwd(),
                    os.path.join(os.getcwd(), "src"),
                ]
                print(
                    f"DEBUG: paths_to_add = {paths_to_add!r}",
                    file=__import__("sys").stderr,
                )
                for path in paths_to_add:
                    print(
                        f"DEBUG: checking path={path!r}, truthy={bool(path)}, in_pythonpath={path in pythonpath.split(os.pathsep)}",
                        file=__import__("sys").stderr,
                    )
                    if path and path not in pythonpath.split(os.pathsep):
                        env["PYTHONPATH"] = f"{path}:{pythonpath}" if pythonpath else path
                        pythonpath = env["PYTHONPATH"]
                        print(
                            f"DEBUG: updated pythonpath to {pythonpath!r}",
                            file=__import__("sys").stderr,
                        )

                print(
                    f"DEBUG: final PYTHONPATH = {env.get('PYTHONPATH')!r}",
                    file=__import__("sys").stderr,
                )
                print(
                    f"DEBUG: calling orig_run with env['PYTHONPATH']={env.get('PYTHONPATH')!r}",
                    file=__import__("sys").stderr,
                )

                result = orig_run(
                    cmd,
                    capture_output=capture_output,
                    text=text,
                    timeout=timeout,
                    cwd=cwd,
                    check=check,
                    input=input,
                    env=env,
                )

                print(
                    f"DEBUG: orig_run returned with returncode={result.returncode}",
                    file=__import__("sys").stderr,
                )
                return result

            # Stubbed commands
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
                if isinstance(cmd, (list, tuple)) and "status" in cmd and "--porcelain" in cmd:
                    out_text = ""  # 変更なし
                elif isinstance(cmd, (list, tuple)) and "rev-parse" in cmd:
                    out_text = "main"
                elif isinstance(cmd, (list, tuple)) and "merge-base" in cmd:
                    out_text = "abc123"
                else:
                    out_text = ""
            elif program == "gh":
                if isinstance(cmd, (list, tuple)) and len(cmd) >= 3 and cmd[1] == "auth" and cmd[2] == "status":
                    # 認証なしをシミュレーション（トークン未設定のテストを通すため）
                    return types.SimpleNamespace(stdout="", stderr="not logged in", returncode=1)
                if isinstance(cmd, (list, tuple)) and len(cmd) >= 3 and cmd[1] == "pr" and cmd[2] == "checks":
                    # タブ区切り形式の一例（PASS）
                    out_text = "CI / build\tPASS\t1m\thttps://example/check\n"
                elif isinstance(cmd, (list, tuple)) and len(cmd) >= 3 and cmd[1] == "run" and cmd[2] == "list":
                    out_text = "[]"
                elif isinstance(cmd, (list, tuple)) and len(cmd) >= 3 and cmd[1] == "run" and cmd[2] == "view" and "--json" in cmd:
                    out_text = '{"jobs":[]}'
                elif isinstance(cmd, (list, tuple)) and len(cmd) >= 2 and cmd[1] == "api":
                    # zip ログ取得など。text=False の呼び出しにも対応
                    pass  # 出力は下で text フラグに応じて生成
                else:
                    out_text = ""
            else:  # gemini/codex/uv
                # --version チェックや exec をダミー成功
                out_text = ""

            if isinstance(cmd, (list, tuple)) and len(cmd) >= 2 and cmd[0] == "gh" and cmd[1] == "api":
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


@pytest.fixture(autouse=True)
def _apply_graphrag_mock_to_compatibility_tests(request, monkeypatch):
    """Automatically apply GraphRAGMCPIntegration mock to compatibility tests."""
    # Only apply to tests in test_graphrag_compatibility.py
    if "test_graphrag_compatibility.py" in request.node.fspath.strpath:
        from unittest.mock import Mock

        # Create a mock GraphRAGMCPIntegration class
        mock_integration_class = Mock()

        # Create a mock instance with the required methods
        mock_instance = Mock()

        # Use side_effect to return different session IDs for different calls
        call_count = {"count": 0}

        def create_session_side_effect(repo_path):
            call_count["count"] += 1
            return f"session_test_{call_count['count']:03d}"

        mock_instance.create_session.side_effect = create_session_side_effect
        mock_instance.get_session_context.return_value = {
            "session_id": "session_test_001",
            "repo_path": "/test/repo",
        }

        # Return different repo labels based on session_id
        def get_repo_label_side_effect(session_id):
            # Extract number from session_id like "session_test_001" -> "001"
            if session_id.startswith("session_test_"):
                num = session_id.split("_")[-1]
                return f"Repo_TEST{num}"
            return "Repo_TEST123"

        mock_instance.get_repo_label_for_session.side_effect = get_repo_label_side_effect
        mock_instance.get_repository_label.return_value = "Repo_TEST123"
        mock_instance.ensure_ready.return_value = True

        mock_integration_class.return_value = mock_instance

        # Patch the GraphRAGMCPIntegration in the module where it's imported
        import src.auto_coder.graphrag_mcp_integration as grag_module

        monkeypatch.setattr(grag_module, "GraphRAGMCPIntegration", mock_integration_class)


@pytest.fixture
def mock_graphrag_integration(monkeypatch):
    """Mock GraphRAGMCPIntegration to avoid Docker dependency in tests."""
    from unittest.mock import Mock

    # Create a mock GraphRAGMCPIntegration class
    mock_integration_class = Mock()

    # Create a mock instance with the required methods
    mock_instance = Mock()
    mock_instance.create_session.return_value = "session_test_123"
    mock_instance.get_session_context.return_value = {
        "session_id": "session_test_123",
        "repo_path": "/test/repo",
    }
    mock_instance.get_repo_label_for_session.return_value = "Repo_TEST123"

    mock_instance.get_repository_label.return_value = "Repo_TEST123"
    mock_instance.ensure_ready.return_value = True

    mock_integration_class.return_value = mock_instance

    # Patch the GraphRAGMCPIntegration in the module where it's imported
    import src.auto_coder.graphrag_mcp_integration as grag_module

    monkeypatch.setattr(grag_module, "GraphRAGMCPIntegration", mock_integration_class)

    return mock_integration_class


@pytest.fixture
def isolated_graphrag_session(mock_graphrag_integration):
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
    from unittest.mock import Mock

    from src.auto_coder.graphrag_mcp_integration import BackwardCompatibilityLayer, GraphRAGMCPIntegration

    # Get the mocked GraphRAGMCPIntegration (already patched by autouse fixture)
    mock_integration = GraphRAGMCPIntegration()

    # Setup existing behavior for compatibility tests
    compat_layer = BackwardCompatibilityLayer(mock_integration)
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
    mock_tool.impact_analysis = Mock(return_value={"affected_symbols": [], "affected_files": []})
    mock_tool.semantic_code_search = Mock(return_value={"symbols": []})
    mock_tool.semantic_code_search_in_collection = Mock(return_value={"symbols": []})
    return mock_tool


# Backend Manager Test Fixtures


@pytest.fixture(autouse=True)
def mock_backend_manager():
    """Mock LLM backend manager for testing."""
    from unittest.mock import Mock

    # Reset singleton before setting up mock
    LLMBackendManager.reset_singleton()

    # Create a mock gemini client
    mock_gemini_client = Mock()
    mock_gemini_client.model_name = "gemini-2.5-pro"

    # Create mock backend manager
    mock_manager = Mock()
    mock_manager.get_last_backend_and_model.return_value = ("gemini", "gemini-2.5-pro")
    mock_manager._run_llm_cli.return_value = "Test response"

    # Initialize the singleton with our mock
    get_llm_backend_manager(
        default_backend="gemini",
        default_client=mock_gemini_client,
        factories={"gemini": lambda: mock_gemini_client},
    )

    return mock_manager

"""
Pytest configuration and fixtures for Auto-Coder tests.
"""

import atexit
import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

# Ensure 'src' directory is on sys.path so 'auto_coder' package is importable everywhere
_repo_root = Path(__file__).resolve().parents[1]
_src_path = _repo_root / "src"
if str(_src_path) not in sys.path:
    sys.path.insert(0, str(_src_path))

from src.auto_coder.automation_engine import AutomationEngine
from src.auto_coder.backend_manager import LLMBackendManager, get_llm_backend_manager
from src.auto_coder.gemini_client import GeminiClient
from src.auto_coder.llm_backend_config import reset_llm_config
from src.auto_coder.util.gh_cache import GitHubClient


# Test stabilization: eliminate external environment variables and user HOME influence (to ensure consistent CLI behavior)
@pytest.fixture(autouse=True)
def _clear_sensitive_env(monkeypatch, request):
    import tempfile

    # Skip HOME mocking for tests that need real HOME directory
    if "_use_real_home" in request.fixturenames:
        # Only clear sensitive env vars, keep real HOME
        for key in ("GITHUB_TOKEN", "GEMINI_API_KEY"):
            monkeypatch.delenv(key, raising=False)
        return

    # Clear influential environment variables
    for key in ("GITHUB_TOKEN", "GEMINI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    # Disable auto-update to avoid noisy stderr output that can break JSON parsing in CLI tests
    monkeypatch.setenv("AUTO_CODER_DISABLE_AUTO_UPDATE", "1")
    # Switch home directory to temporary directory, blocking effects of real files like ~/.config/gh/hosts.yml
    tmp_home = tempfile.mkdtemp(prefix="ac_test_home_")
    monkeypatch.setenv("HOME", tmp_home)


@pytest.fixture(autouse=True)
def _reset_github_client_singleton():
    """Reset GitHubClient singleton between tests to ensure isolation."""
    GitHubClient.reset_singleton()
    yield
    GitHubClient.reset_singleton()


@pytest.fixture(autouse=True)
def _reset_llm_config_singleton():
    """Reset LLM config singleton between tests to ensure isolation."""
    reset_llm_config()
    yield
    reset_llm_config()


@pytest.fixture
def _use_custom_subprocess_mock():
    """Indicate that this test uses custom subprocess mocking.

    When this fixture is used, the stub_git_and_gh_commands fixture will skip
    patching subprocess.run, allowing the test to use its own mock.
    """
    pass


@pytest.fixture(autouse=True)
def _cleanup_loguru_handlers():
    """Clean up loguru handlers after each test to prevent queue hangs."""
    from loguru import logger as loguru_logger

    # Store all handlers before test
    handlers_before = list(loguru_logger._core.handlers.values())

    yield

    # Remove ALL handlers after each test
    loguru_logger.remove()

    # Ensure all writer threads are terminated by clearing the handlers' queues
    for handler in handlers_before:
        try:
            # Access the queue if it exists and close it
            if hasattr(handler, "_queue"):
                queue = handler._queue
                if hasattr(queue, "close"):
                    queue.close()
                if hasattr(queue, "join"):
                    try:
                        queue.join()
                    except Exception:
                        pass
        except Exception:
            pass

    # Re-add minimal console handler to avoid issues in subsequent tests
    # Use enqueue=False to prevent any background threads
    loguru_logger.add(
        sys.stderr,
        format="{time:HH:mm:ss} | {level: <8} | {name}:{line} - {message}",
        level="WARNING",
        enqueue=False,
        colorize=True,
        catch=True,
    )

    # Register final cleanup at exit
    atexit.register(lambda: loguru_logger.remove())


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
    client = Mock()
    client.token = mock_github_token
    # Set get_open_prs_json to return an empty list by default to prevent iteration errors
    client.get_open_prs_json.return_value = []
    # Set get_open_issues to return an empty list by default to prevent iteration errors
    client.get_open_issues.return_value = []
    # Set get_open_issues_json to return an empty list by default to prevent iteration errors
    # This method was added to batch fetch issue details via GraphQL
    client.get_open_issues_json.return_value = []
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


@pytest.fixture
def _use_custom_subprocess_mock():
    """Marker fixture to indicate test uses custom subprocess mocking.

    Tests that use this fixture will bypass the stub_git_and_gh_commands fixture
    to allow for custom subprocess.run mocking.
    """
    pass


@pytest.fixture
def _use_real_streaming_logic():
    """Marker fixture to indicate test should use real streaming logic.

    Tests that use this fixture will use the original _should_stream_output
    implementation instead of the stubbed version. This is useful for tests
    that specifically validate debugger detection and streaming logic.
    """
    pass


# Stub to prevent actual git/gh commands from being executed during testing
@pytest.fixture(autouse=True)
def stub_git_and_gh_commands(monkeypatch, request):
    import subprocess
    import types

    # Use real streaming logic for tests that need it
    use_real_streaming = "_use_real_streaming_logic" in request.fixturenames

    # Skip command stubbing for tests that need real commands
    skip_stub = False
    if "_use_real_commands" in request.fixturenames:
        print("DEBUG: Skipping stub - _use_real_commands fixture found", file=__import__("sys").stderr)
        skip_stub = True

    # Skip command stubbing for tests that use custom subprocess mocking
    if "_use_custom_subprocess_mock" in request.fixturenames:
        print("DEBUG: Skipping stub - _use_custom_subprocess_mock fixture found", file=__import__("sys").stderr)
        skip_stub = True

    # Disable streaming for git commands to avoid queue timeout issues
    from src.auto_coder.utils import CommandExecutor

    orig_should_stream = CommandExecutor._should_stream_output

    def patched_should_stream(stream_output):
        # For tests that need to validate the actual streaming logic (e.g., debugger detection),
        # use the original implementation
        if use_real_streaming:
            return orig_should_stream(stream_output)

        # If streaming is requested for a git command, disable it in tests
        if stream_output is None:
            # Check if we're in a context where git commands are being called
            # For tests, always disable streaming to avoid queue issues
            return False
        return orig_should_stream(stream_output)

    if not skip_stub:
        CommandExecutor._should_stream_output = staticmethod(patched_should_stream)
    else:
        # Restore original if it was previously patched
        CommandExecutor._should_stream_output = staticmethod(orig_should_stream)

    if skip_stub:
        # Don't patch subprocess.run - let the test use its own mock
        yield
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
        # bytes output
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
        stdout=None,
        stderr=None,
        **kwargs,
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
            if program not in ("git", "gh", "gemini", "codex", "uv", "node", "uname"):
                return orig_run(
                    cmd,
                    capture_output=capture_output,
                    text=text,
                    timeout=timeout,
                    cwd=cwd,
                    check=check,
                    input=input,
                    env=env,
                    stdout=stdout,
                    stderr=stderr,
                    **kwargs,
                )

            # Default success response
            out_text = ""

            if program == "git":
                # Minimal normal output for representative calls
                if isinstance(cmd, (list, tuple)) and "status" in cmd and "--porcelain" in cmd:
                    out_text = ""  # No changes
                elif isinstance(cmd, (list, tuple)) and "rev-parse" in cmd:
                    out_text = "main"
                elif isinstance(cmd, (list, tuple)) and "merge-base" in cmd:
                    out_text = "abc123"
                else:
                    out_text = ""
            elif program == "gh":
                if isinstance(cmd, (list, tuple)) and len(cmd) >= 3 and cmd[1] == "auth" and cmd[2] == "status":
                    # Simulate no authentication (to pass tests without token)
                    return types.SimpleNamespace(stdout="", stderr="not logged in", returncode=1)
                if isinstance(cmd, (list, tuple)) and len(cmd) >= 3 and cmd[1] == "pr" and cmd[2] == "checks":
                    # Example of tab-separated format (PASS)
                    out_text = "CI / build\tPASS\t1m\thttps://example/check\n"
                elif isinstance(cmd, (list, tuple)) and len(cmd) >= 3 and cmd[1] == "run" and cmd[2] == "list":
                    out_text = "[]"
                elif isinstance(cmd, (list, tuple)) and len(cmd) >= 3 and cmd[1] == "run" and cmd[2] == "view" and "--json" in cmd:
                    out_text = '{"jobs":[]}'
                elif isinstance(cmd, (list, tuple)) and len(cmd) >= 2 and cmd[1] == "api":
                    # Get zip logs, etc. Also supports text=False calls
                    pass  # Output will be generated below based on text flag
                else:
                    out_text = ""
            elif program == "node":
                # Simulate Node.js CLI behavior for graph-builder commands
                try:
                    import json as _json
                    from pathlib import Path as _Path
                except Exception:
                    pass

                # Version check
                if (isinstance(cmd, (list, tuple)) and "--version" in cmd) or (isinstance(cmd, str) and "--version" in cmd):
                    out_text = "v20.0.0\n"
                else:
                    # Help check or actual scan
                    argv = cmd if isinstance(cmd, (list, tuple)) else cmd.split()
                    if "scan" in argv and "--help" in argv:
                        out_text = "--languages, --project, --out\n"
                    elif "scan" in argv:
                        # On scan, create out/graph-data.json with minimal content
                        try:
                            if "--out" in argv:
                                out_idx = argv.index("--out") + 1
                                if out_idx < len(argv):
                                    out_dir = _Path(argv[out_idx])
                                    out_dir.mkdir(parents=True, exist_ok=True)
                                    (_Path(out_dir) / "graph-data.json").write_text(_json.dumps({"nodes": [], "edges": []}))
                        except Exception:
                            pass
                        out_text = ""
                # For node we return here
                if text:
                    stdout, stderr = _as_text_or_bytes(out_text, True)
                else:
                    stdout, stderr = _as_text_or_bytes(out_text, False)
                return types.SimpleNamespace(stdout=stdout, stderr=stderr, returncode=0)
            elif program == "uname":
                out_text = "x86_64\n"
            else:  # gemini/codex/uv
                # Dummy success for --version check and exec
                out_text = ""

            if isinstance(cmd, (list, tuple)) and len(cmd) >= 2 and cmd[0] == "gh" and cmd[1] == "api":
                # API calls are OK with binary or text empty output
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
            # Unexpected cases fall back to original run
            return orig_run(
                cmd,
                capture_output=capture_output,
                text=text,
                timeout=timeout,
                cwd=cwd,
                check=check,
                input=input,
                env=env,
                stdout=stdout,
                stderr=stderr,
                **kwargs,
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
        **kwargs,
    ):
        try:
            program = cmd[0] if isinstance(cmd, (list, tuple)) and cmd else None
            if program in ("git", "gh", "gemini", "codex", "uv", "node"):

                class MockStream:
                    def __init__(self, content):
                        import io

                        if isinstance(content, bytes):
                            self._stream = io.BytesIO(content)
                            self._is_bytes = True
                        else:
                            self._stream = io.StringIO(content)
                            self._is_bytes = False
                        self._closed = False

                    def readline(self, *args, **kwargs):
                        if self._closed:
                            return b"" if self._is_bytes else ""
                        return self._stream.readline(*args, **kwargs)

                    def __iter__(self):
                        return self

                    def __next__(self):
                        if self._closed:
                            raise StopIteration
                        line = self.readline()
                        if not line:
                            raise StopIteration
                        return line

                    def read(self, *args):
                        if self._closed:
                            return b"" if self._is_bytes else ""
                        return self._stream.read(*args)

                    def close(self):
                        self._closed = True

                    def __enter__(self):
                        return self

                    def __exit__(self, exc_type, exc_val, exc_tb):
                        self.close()
                        return False

                class DummyPopen:
                    def __init__(self):
                        # Create streams that have readline() method like real file objects
                        self.stdout = MockStream("")
                        self.stderr = MockStream("")
                        self.pid = 0
                        self.returncode = None

                    def wait(self, timeout=None):
                        if self.returncode is None:
                            self.returncode = 0
                        return self.returncode

                    def poll(self):
                        return self.returncode

                    def __enter__(self):
                        return self

                    def __exit__(self, exc_type, exc_val, exc_tb):
                        if hasattr(self.stdout, "close"):
                            self.stdout.close()
                        if hasattr(self.stderr, "close"):
                            self.stderr.close()
                        return False

                return DummyPopen()
            # universal_newlines is synonymous with text in Python3.12. Avoid conflicts with both specified
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
                **kwargs,
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
                **kwargs,
            )

    if not skip_stub:
        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(subprocess, "Popen", fake_popen)

    # Cleanup: restore original function after test
    yield

    # Restore the original _should_stream_output method if we modified it
    if not skip_stub:
        CommandExecutor._should_stream_output = orig_should_stream


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


@pytest.fixture
def mock_backend_manager():
    """Mock LLM backend manager for testing.

    Note: Only applied to tests that explicitly request this fixture.
    E2E tests that don't use LLM backends should not request this fixture.
    """
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


# GraphRAG Index Manager Test Fixtures


@pytest.fixture
def graph_builder_structure(tmp_path):
    """Create a minimal valid graph-builder structure for testing.

    Args:
        tmp_path: pytest tmp_path fixture

    Returns:
        Path to the created graph-builder directory
    """
    graph_builder_dir = tmp_path / "graph-builder"
    graph_builder_dir.mkdir()
    (graph_builder_dir / "src").mkdir()
    (graph_builder_dir / "src" / "cli_python.py").touch()
    return graph_builder_dir


@pytest.fixture
def graph_builder_typescript_structure(tmp_path):
    """Create a TypeScript graph-builder structure for testing.

    Args:
        tmp_path: pytest tmp_path fixture

    Returns:
        Path to the created graph-builder directory with TypeScript CLI
    """
    graph_builder_dir = tmp_path / "graph-builder"
    graph_builder_dir.mkdir()
    (graph_builder_dir / "src").mkdir()
    (graph_builder_dir / "dist").mkdir()
    (graph_builder_dir / "dist" / "cli.js").touch()
    return graph_builder_dir


@pytest.fixture
def graph_builder_bundle_structure(tmp_path):
    """Create a bundled TypeScript graph-builder structure for testing.

    Args:
        tmp_path: pytest tmp_path fixture

    Returns:
        Path to the created graph-builder directory with bundled TypeScript CLI
    """
    graph_builder_dir = tmp_path / "graph-builder"
    graph_builder_dir.mkdir()
    (graph_builder_dir / "src").mkdir()
    (graph_builder_dir / "dist").mkdir()
    (graph_builder_dir / "dist" / "cli.bundle.js").touch()
    return graph_builder_dir


@pytest.fixture
def graph_rag_index_manager(tmp_path):
    """Create a GraphRAGIndexManager instance for testing.

    Args:
        tmp_path: pytest tmp_path fixture

    Returns:
        GraphRAGIndexManager instance
    """
    from src.auto_coder.graphrag_index_manager import GraphRAGIndexManager

    # Create a temporary state file
    state_file = tmp_path / "test_state.json"
    return GraphRAGIndexManager(repo_path=str(tmp_path), index_state_file=str(state_file))


@pytest.fixture
def graph_rag_index_manager_with_override(graph_builder_structure):
    """Create a GraphRAGIndexManager with graph-builder path override.

    Args:
        graph_builder_structure: graph_builder_structure fixture

    Returns:
        GraphRAGIndexManager instance with override set
    """
    from src.auto_coder.graphrag_index_manager import GraphRAGIndexManager

    manager = GraphRAGIndexManager()
    manager.set_graph_builder_path_for_testing(graph_builder_structure)
    return manager


def pytest_sessionfinish(session, exitstatus):
    """
    Called after the entire test session finishes.
    Collects log if running via simple pytest command (not via local_test_log_collector.py).
    """
    # Check if we are running via local_test_log_collector.py by checking an env var or arg
    # Ideally, local_test_log_collector.py could set an env var, but we didn't add that.
    # However, if we blindly save a log here, we might duplicate it if the runner also saves it.
    # But better duplicate than missing.
    # Or we can check if we are in a subprocess of the runner?
    # Let's check environment variable that we can set in runner, OR just always save
    # and maybe overwrite or have two files.
    # Given the user issue "log collection not functioning", redundancy is safer.
    # But duplication is annoying.
    # Let's try to detect if we should run.
    # Actually, the runner SAVES the stdout/stderr. Inside pytest, we can't easily access full stdout/stderr
    # of the session itself including what happened before this hook.
    # But we CAN save what pytest knows: the test results.
    # However, the requirement seems to be about the "test log" JSON format.
    # If the user runs `pytest`, they expect the JSON log to be created.

    # We need to reconstruct the LogEntry.
    try:
        import platform
        from datetime import datetime

        from src.auto_coder.git_info import get_current_repo_name
        from src.auto_coder.log_utils import LogEntry, get_test_log_dir

        repo_name = get_current_repo_name()
        if not repo_name:
            return

        timestamp = datetime.now()
        # We don't have full stdout/stderr here easily unless we used capsys globally and somehow kept it.
        # But we can save a "minimal" log entry indicating completion and exit code.
        # This at least provides a record.

        # To avoid duplication with local_test_log_collector.py, we can check if an env var is set.
        # We will modify local_test_log_collector.py to set AUTO_CODER_LOG_COLLECTOR_ACTIVE=1 later if needed.
        # For now, let's assume we want to cover the case where it's NOT set.
        if os.environ.get("AUTO_CODER_LOG_COLLECTOR_ACTIVE") == "1":
            return

        log_dir = get_test_log_dir(repo_name)
        log_filename = f"{timestamp.strftime('%Y%m%d_%H%M%S')}_pytest_direct.json"

        # Try to gather some summary info
        # We can't easily get the full stdout of the process *running* us.
        # But we can log that it happened.
        log_entry = LogEntry(
            ts=timestamp.isoformat(),
            source="pytest_hook",
            repo=repo_name,
            command=" ".join(sys.argv),
            exit_code=exitstatus,
            stdout="(Captured via pytest hook - full output unavailable)",
            stderr="",
            file=None,
            meta={
                "os": platform.system(),
                "python_version": platform.python_version(),
            },
        )
        log_entry.save(log_dir, log_filename)
        # We print to stderr so it doesn't interfere with json output if any
        # print(f"Test log saved to: {log_dir / log_filename}", file=sys.stderr)

    except Exception:
        # Don't fail the test run just because logging failed
        pass

"""
Utility classes for Auto-Coder automation engine.
"""

import os
import queue
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from .logger_config import get_logger
from .progress_footer import get_progress_footer
from .security_utils import redact_string
from .test_log_utils import extract_first_failed_test

logger = get_logger(__name__)


def get_target_container(config: Any) -> Optional[str]:
    """Determine the target container name based on environment and config.

    When running in a dedicated auto-coder container (AM_I_AUTOCODER_CONTAINER=true),
    the target container name is derived from the repository name as auto-coder-[repo].
    """
    if os.environ.get("AM_I_AUTOCODER_CONTAINER") == "true":
        # We use Any for config to avoid circular imports
        repo_name = getattr(config, "repo_name", None) or os.environ.get("REPO_NAME")
        if repo_name:
            repo_segment = repo_name.split("/")[-1]
            return f"auto-coder-{repo_segment}"
        logger.warning("AM_I_AUTOCODER_CONTAINER is true but no repository name found.")
    return None


def get_pr_author_login(pr_obj: Any) -> Optional[str]:
    """Extract author login from a PR-like object or dict.

    Supports both PyGithub PR objects (with .user.login) and dictionaries
    returned from GitHubClient (which may use 'author' or 'user').
    """
    try:
        from unittest.mock import MagicMock as _MagicMock
        from unittest.mock import Mock as _Mock

        _mock_types = (_Mock, _MagicMock)
    except Exception:
        _mock_types = tuple()  # type: ignore

    try:
        if isinstance(pr_obj, dict):
            # Try 'author' field first (some custom dicts or GraphQL results)
            author = pr_obj.get("author")
            if isinstance(author, dict):
                return author.get("login")
            if isinstance(author, str):
                return author
            # If not found, try 'user' -> 'login' (GitHub REST API format)
            user = pr_obj.get("user")
            if isinstance(user, dict):
                return user.get("login")
            return None
        else:
            # Handle PyGithub objects or Mocks
            user = getattr(pr_obj, "user", None)
            if user is None or isinstance(user, _mock_types):
                # Fallback for some objects where user is not an attribute but author is
                author = getattr(pr_obj, "author", None)
                if isinstance(author, str):
                    return author
                if author is not None:
                    return getattr(author, "login", None)
            return getattr(user, "login", None) if user is not None else None
    except Exception:
        return None


VERBOSE_ENV_FLAG = "AUTOCODER_VERBOSE"


class TemporaryEnvironment:
    """
    Context manager for temporarily setting environment variables.

    This context manager allows you to set environment variables for the duration
    of a block of code and automatically restore them to their previous state
    (or remove them if they didn't exist before).

    Example:
        with TemporaryEnvironment({"MY_VAR": "value"}):
            # MY_VAR is set in os.environ
            subprocess.run(["my_command"])
        # MY_VAR is restored or removed

    Note: This modifies os.environ directly and ensures cleanup even on exceptions.
    """

    def __init__(self, env_vars: Optional[Dict[str, str]] = None):
        """
        Initialize the temporary environment context manager.

        Args:
            env_vars: Dictionary of environment variables to set temporarily.
                      If None, no environment variables are modified.
        """
        self.env_vars = env_vars or {}
        self._previous_values: Dict[str, Optional[str]] = {}

    def __enter__(self) -> None:
        """Set the temporary environment variables."""
        # Save current values and set new ones
        for key, value in self.env_vars.items():
            self._previous_values[key] = os.environ.get(key)
            os.environ[key] = value

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Restore the previous environment variable values."""
        for key, previous_value in self._previous_values.items():
            if previous_value is None:
                # Variable didn't exist before, remove it
                if key in os.environ:
                    del os.environ[key]
            else:
                # Variable existed before, restore it
                os.environ[key] = previous_value


def is_running_in_container() -> bool:
    """Robustly detect if running inside a container.

    Uses multiple detection methods to determine if we're running in a container:
    1. Check for /.dockerenv file (Docker)
    2. Check for /run/.containerenv file (Podman)
    3. Check for container environment variables
    4. Check for container-specific cgroup entries
    5. Check for container-specific parent process names

    This method is environment-agnostic and works across:
    - Docker containers
    - Podman containers
    - Kubernetes pods
    - GitHub Actions (Linux runners in containers)
    - Other container runtimes

    Returns:
        True if running in a container, False otherwise
    """
    # Method 1: Check for /.dockerenv (Docker)
    try:
        from pathlib import Path

        if Path("/.dockerenv").exists():
            logger.debug("Container detected: /.dockerenv exists")
            return True
    except Exception:
        pass

    # Method 2: Check for /run/.containerenv (Podman)
    try:
        from pathlib import Path

        if Path("/run/.containerenv").exists():
            logger.debug("Container detected: /run/.containerenv exists")
            return True
    except Exception:
        pass

    # Method 3: Check environment variables set by containers
    container_env_vars = [
        "container",  # Docker sets this to "docker" or "docker-runc"
        "DOCKER_CONTAINER",  # Custom env var sometimes used
        "KUBERNETES_SERVICE_HOST",  # Kubernetes
        "KUBERNETES_NAMESPACE",  # Kubernetes
    ]

    for env_var in container_env_vars:
        if os.environ.get(env_var):
            logger.debug(f"Container detected: {env_var} environment variable is set")
            return True

    # Method 4: Check cgroup for container indicators
    try:
        with open("/proc/self/cgroup", "r") as f:
            cgroup_content = f.read()
            # Look for container-specific cgroup entries
            if any(
                marker in cgroup_content
                for marker in [
                    "/docker/",
                    "/docker-",
                    "/kubepods/",
                    "/kubepods/burstable/",
                    "/lxc/",
                    "/containerd/",
                ]
            ):
                logger.debug("Container detected: cgroup contains container markers")
                return True
    except Exception:
        pass

    # Method 5: Check for 1 as init process (typical in containers)
    try:
        with open("/proc/1/cmdline", "r") as f:
            cmdline = f.read()
            # In containers, PID 1 is often the container runtime or the main process
            if cmdline and len(cmdline.split("\x00")) <= 2:
                logger.debug("Container detected: PID 1 has limited command line")
                return True
    except Exception:
        pass

    # Method 6: Check parent process for container indicators
    try:
        with open("/proc/self/stat", "r") as f:
            stat_content = f.read()
            # Extract parent process ID (PPid)
            parts = stat_content.split()
            if len(parts) >= 4:
                ppid = int(parts[3])
                # Check parent's command line
                try:
                    with open(f"/proc/{ppid}/cmdline", "r") as parent_cmdline:
                        parent_cmd = parent_cmdline.read()
                        if any(
                            marker in parent_cmd.lower()
                            for marker in [
                                "dockerd",
                                "containerd",
                                "runc",
                                "podman",
                                "docker-proxy",
                            ]
                        ):
                            logger.debug("Container detected: parent process is container runtime")
                            return True
                except Exception:
                    pass
    except Exception:
        pass

    logger.debug("Not running in a container")
    return False


@dataclass
class CommandResult:
    """Result of a command execution."""

    success: bool
    stdout: str
    stderr: str
    returncode: int


class CommandExecutor:
    """Utility class for executing commands with consistent error handling."""

    # Default timeouts for different command types
    DEFAULT_TIMEOUTS = {
        "git": 120,
        "gh": 60,
        "test": 3600,
        "auggie": 7200,
        "claude": 7200,
        "codex": 7200,
        "gemini": 7200,
        "qwen": 7200,
        "aider": 7200,
        "default": 60,
    }

    DEBUGGER_ENV_MARKERS = (
        "PYDEVD_USE_FRAME_EVAL",
        "PYDEVD_LOAD_VALUES_ASYNC",
        "DEBUGPY_LAUNCHER_PORT",
        "PYDEV_DEBUG",
        "VSCODE_PID",
    )

    # Interval for polling worker queue while streaming output (seconds)
    STREAM_POLL_INTERVAL = 0.2

    @staticmethod
    def is_running_in_debugger() -> bool:
        """Detect if the process is running under a debugger.

        Returns:
            True if running in debugger, False otherwise
        """
        # Detect common debugger environment markers (debugpy, VS Code, PyCharm)
        for marker in CommandExecutor.DEBUGGER_ENV_MARKERS:
            value = os.environ.get(marker, "").strip().lower()
            if value in {"1", "true", "yes"}:
                return True

        # Heuristic: when a debugger is attached (sys.gettrace), favor streaming
        try:
            return sys.gettrace() is not None
        except Exception:
            return False

    @staticmethod
    def _should_stream_output(stream_output: Optional[bool]) -> bool:
        """Determine whether to stream command output in real time."""
        if stream_output is not None:
            return stream_output

        # Allow forcing via env var for manual debugging sessions
        value = os.environ.get("AUTOCODER_STREAM_COMMANDS", "").strip().lower()
        if value in {"1", "true", "yes"}:
            return True

        # Use the debugger detection helper
        return CommandExecutor.is_running_in_debugger()

    @staticmethod
    def _spawn_reader(stream, stream_name: str, out_queue: "queue.Queue[Tuple[str, Optional[str]]]") -> threading.Thread:
        """Spawn a background reader thread for the given stream."""

        def _reader() -> None:
            try:
                for line in iter(stream.readline, ""):
                    out_queue.put((stream_name, line))
            except Exception as exc:  # pragma: no cover - defensive
                logger.error(f"Streaming reader failed for {stream_name}: {exc}")
            finally:
                out_queue.put((stream_name, None))

        thread = threading.Thread(target=_reader, name=f"CommandStream-{stream_name}", daemon=True)
        thread.start()
        return thread

    @classmethod
    def _run_with_streaming(
        cls,
        cmd: List[str],
        timeout: Optional[int],
        cwd: Optional[str],
        env: Optional[Dict[str, str]],
        on_stream: Optional[Callable[[str, str], None]] = None,
        dot_format: bool = False,
        idle_timeout: Optional[int] = None,
        log_output: bool = True,
    ) -> Tuple[int, str, str]:
        """Run a command while streaming stdout/stderr to the logger.

        on_stream: optional callback invoked for each chunk (stream_name, chunk).
        The callback may raise to abort the process early; the exception is propagated.
        log_output: if False, suppress logging of stdout/stderr chunks (default: True).
        """
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=cwd,
            env=env,
        )

        stdout_lines: List[str] = []
        stderr_lines: List[str] = []

        streams_active: set[str] = set()
        output_queue: "queue.Queue[Tuple[str, Optional[str]]]" = queue.Queue()
        readers: List[threading.Thread] = []

        if process.stdout is not None:
            streams_active.add("stdout")
            readers.append(cls._spawn_reader(process.stdout, "stdout", output_queue))
        if process.stderr is not None:
            streams_active.add("stderr")
            readers.append(cls._spawn_reader(process.stderr, "stderr", output_queue))

        start = time.monotonic()
        last_output_time = time.monotonic()
        dots_printed = 0

        try:
            while True:
                now = time.monotonic()
                if timeout is not None:
                    elapsed = now - start
                    remaining = timeout - elapsed
                    if remaining <= 0:
                        process.kill()
                        raise subprocess.TimeoutExpired(cmd, timeout, "".join(stdout_lines), "".join(stderr_lines))
                else:
                    remaining = None

                # Check idle timeout
                if idle_timeout is not None:
                    idle_time = now - last_output_time
                    if idle_time > idle_timeout:
                        process.kill()
                        raise subprocess.TimeoutExpired(cmd, idle_timeout, "".join(stdout_lines), "".join(stderr_lines))  # Using idle_timeout as timeout value for exception

                poll_interval = cls.STREAM_POLL_INTERVAL
                if remaining is not None:
                    poll_interval = min(poll_interval, remaining)

                # Tick the progress footer spinner
                try:
                    get_progress_footer().tick()
                except Exception:
                    # Don't let footer errors crash the command execution
                    pass

                try:
                    stream_name, chunk = output_queue.get(timeout=poll_interval)
                    last_output_time = time.monotonic()  # Reset idle timer on output
                    if chunk is None:
                        streams_active.discard(stream_name)
                    else:
                        if stream_name == "stdout":
                            stdout_lines.append(chunk)
                        else:
                            stderr_lines.append(chunk)

                        # Skip empty lines and don't log them
                        stripped_chunk = chunk.rstrip("\n")
                        if stripped_chunk:
                            verbose_requested = os.environ.get(VERBOSE_ENV_FLAG, "").strip().lower() in {
                                "1",
                                "true",
                                "yes",
                            }
                            if dot_format and not verbose_requested:
                                # Print dot one line above to avoid hiding ProgressStage footer
                                if sys.stderr.isatty():
                                    # Use ANSI escape sequences for TTY
                                    cols = shutil.get_terminal_size().columns
                                    # Calculate position (1-based)
                                    position = (dots_printed % cols) + 1
                                    # Calculate cycle (0 for ., 1 for *)
                                    cycle = (dots_printed // cols) % 2
                                    char = "." if cycle == 0 else "*"

                                    sys.stderr.write("\033[s")  # Save cursor position
                                    sys.stderr.write("\033[1A")  # Move cursor up one line
                                    sys.stderr.write(f"\033[{position}G")  # Move to correct column
                                    sys.stderr.write(char)  # Print char
                                    sys.stderr.write("\033[u")  # Restore cursor position
                                    dots_printed += 1
                                else:
                                    # Fallback for non-TTY environments
                                    print(".", end="", file=sys.stderr)
                                sys.stderr.flush()
                            elif log_output:
                                # Also output stderr at INFO level
                                # depth=2 to show the caller of _run_with_streaming
                                # Redact sensitive information from output logs
                                logger.opt(depth=2).info(redact_string(stripped_chunk))

                        # Optional per-chunk callback for early aborts
                        if on_stream is not None:
                            try:
                                on_stream(stream_name, chunk)
                            except Exception:
                                try:
                                    process.kill()
                                except Exception:
                                    pass
                                raise
                except queue.Empty:
                    pass

                if process.poll() is not None and not streams_active and output_queue.empty():
                    break

            return_code = process.returncode
            stdout = "".join(stdout_lines)
            stderr = "".join(stderr_lines)
            return return_code, stdout, stderr
        except KeyboardInterrupt:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            raise
        finally:
            # Ensure process is terminated to unblock pipes
            try:
                if process.poll() is None:
                    try:
                        process.terminate()
                    except Exception:
                        pass
                    try:
                        process.wait(timeout=0.5)
                    except Exception:
                        try:
                            process.kill()
                        except Exception:
                            pass
                        try:
                            process.wait(timeout=0.5)
                        except Exception:
                            pass
            except Exception:
                pass
            # Join reader threads (daemon) briefly; do not hard-block
            for reader in readers:
                try:
                    reader.join(timeout=1)
                except Exception:
                    pass
            # Avoid explicit close() on pipes to prevent rare blocking on some platforms

    @classmethod
    def run_command(
        cls,
        cmd: List[str],
        timeout: Optional[int] = None,
        cwd: Optional[str] = None,
        stream_output: Optional[bool] = None,
        env: Optional[Dict[str, str]] = None,
        env_overrides: Optional[Dict[str, str]] = None,
        on_stream: Optional[Callable[[str, str], None]] = None,
        dot_format: bool = False,
        idle_timeout: Optional[int] = None,
    ) -> CommandResult:
        """Run a command with consistent error handling."""
        if timeout is None:
            # Auto-detect timeout based on command type
            cmd_type = cmd[0] if cmd else "default"
            timeout = cls.DEFAULT_TIMEOUTS.get(cmd_type, cls.DEFAULT_TIMEOUTS["default"])

        command_display = shlex.join(cmd) if cmd else ""
        # Redact potentially sensitive information from command display
        command_display = redact_string(command_display)

        should_stream = cls._should_stream_output(stream_output)
        log_message = f"Executing command (timeout={timeout}s, stream={should_stream}): {command_display}"

        verbose_requested = os.environ.get(VERBOSE_ENV_FLAG, "").strip().lower() in {
            "1",
            "true",
            "yes",
        }

        if verbose_requested:
            logger.info(log_message)
        else:
            logger.debug(log_message)

        # Merge override-only environments with provided env (or os.environ when absent).
        effective_env = env
        if env_overrides:
            base_env = env.copy() if env is not None else os.environ.copy()
            base_env.update(env_overrides)
            effective_env = base_env

        try:
            if should_stream:
                return_code, stdout, stderr = cls._run_with_streaming(
                    cmd,
                    timeout,
                    cwd,
                    effective_env,
                    on_stream,
                    dot_format,
                    idle_timeout=idle_timeout,
                    log_output=True,
                )
            else:
                # Use _run_with_streaming even when not streaming to logger,
                # to ensure progress footer spinner is ticked and timeouts/interruption are handled consistently.
                return_code, stdout, stderr = cls._run_with_streaming(
                    cmd,
                    timeout,
                    cwd,
                    effective_env,
                    on_stream,
                    dot_format,
                    idle_timeout=idle_timeout,
                    log_output=False,
                )

            success = return_code == 0

            return CommandResult(success=success, stdout=stdout, stderr=stderr, returncode=return_code)

        except subprocess.TimeoutExpired:
            logger.error(f"Command timed out after {timeout}s: {' '.join(cmd)}")
            return CommandResult(
                success=False,
                stdout="",
                stderr=f"Command timed out after {timeout}s",
                returncode=-1,
            )
        except Exception as e:
            logger.error(f"Command execution failed: {' '.join(cmd)}: {e}")
            return CommandResult(success=False, stdout="", stderr=str(e), returncode=-1)


def change_fraction(old: str, new: str) -> float:
    """Return fraction of change between two strings (0.0..1.0).

    For performance optimization, the comparison targets the smaller of either
    the trailing "20 lines" or "1000 characters".
    Implementation: Extract a trailing window from each string, then calculate
    the similarity using difflib.SequenceMatcher and return change = 1 - ratio.
    """
    try:
        import difflib

        if old is None and new is None:
            return 0.0

        def tail_window(s: str) -> str:
            if not s:
                return ""
            # Trailing 20 lines
            lines = s.splitlines()
            tail_by_lines = "\n".join(lines[-20:])
            # Trailing 1000 characters
            tail_by_chars = s[-1000:]
            # Use the shorter one
            return tail_by_lines if len(tail_by_lines) <= len(tail_by_chars) else tail_by_chars

        old_s = old or ""
        new_s = new or ""
        if old_s == new_s:
            return 0.0

        old_win = tail_window(old_s)
        new_win = tail_window(new_s)

        ratio = difflib.SequenceMatcher(None, old_win, new_win).ratio()
        return max(0.0, 1.0 - ratio)
    except Exception:
        # Conservative fallback: assume large change
        return 1.0


def log_action(action: str, success: bool = True, details: str = "") -> str:
    """Standardized action logging."""
    message = action
    if details:
        message += f": {details}"

    if success:
        logger.opt(depth=1).info(message)
    else:
        logger.opt(depth=1).error(message)
    return message

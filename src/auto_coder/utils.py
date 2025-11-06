"""
Utility classes for Auto-Coder automation engine.
"""

import os
import queue
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from .logger_config import get_logger

logger = get_logger(__name__)


VERBOSE_ENV_FLAG = "AUTOCODER_VERBOSE"


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
            if any(marker in cgroup_content for marker in [
                "/docker/",
                "/docker-",
                "/kubepods/",
                "/kubepods/burstable/",
                "/lxc/",
                "/containerd/",
            ]):
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
                        if any(marker in parent_cmd.lower() for marker in [
                            "dockerd",
                            "containerd",
                            "runc",
                            "podman",
                            "docker-proxy",
                        ]):
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
    ) -> Tuple[int, str, str]:
        """Run a command while streaming stdout/stderr to the logger.

        on_stream: optional callback invoked for each chunk (stream_name, chunk).
        The callback may raise to abort the process early; the exception is propagated.
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

        try:
            while True:
                if timeout is not None:
                    elapsed = time.monotonic() - start
                    remaining = timeout - elapsed
                    if remaining <= 0:
                        process.kill()
                        raise subprocess.TimeoutExpired(cmd, timeout, "".join(stdout_lines), "".join(stderr_lines))

                poll_interval = min(cls.STREAM_POLL_INTERVAL, remaining) if timeout is not None else cls.STREAM_POLL_INTERVAL

                try:
                    stream_name, chunk = output_queue.get(timeout=poll_interval)
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
                            # Also output stderr at INFO level
                            # depth=2 to show the caller of _run_with_streaming
                            logger.opt(depth=2).info(stripped_chunk)

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
        on_stream: Optional[Callable[[str, str], None]] = None,
    ) -> CommandResult:
        """Run a command with consistent error handling."""
        if timeout is None:
            # Auto-detect timeout based on command type
            cmd_type = cmd[0] if cmd else "default"
            timeout = cls.DEFAULT_TIMEOUTS.get(cmd_type, cls.DEFAULT_TIMEOUTS["default"])

        command_display = shlex.join(cmd) if cmd else ""
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

        try:
            if should_stream:
                return_code, stdout, stderr = cls._run_with_streaming(cmd, timeout, cwd, env, on_stream)
            else:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=cwd,
                    env=env,
                )
                return_code = result.returncode
                stdout = result.stdout
                stderr = result.stderr

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


def extract_first_failed_test(stdout: str, stderr: str) -> Optional[str]:
    """Extract and return the "path of the first failed test file" from test output.

    Two-stage detection method:
    1. First, determine which test library failed
    2. Then, extract the failed test file using patterns specific to that test library

    Supported formats:
    - pytest: End summary "FAILED tests/test_x.py::test_y - ..." etc.
    - pytest: Traceback "tests/test_x.py:123: in test_y" etc.
    - Playwright: Any log "e2e/foo/bar.spec.ts:16:5" etc.
    - Vitest: "FAIL src/foo.test.ts" etc.

    Returns the found path. May return a candidate even if existence check fails (interpreted by caller).
    """
    import re

    ansi_escape = re.compile(r"\x1b\[[0-9;]*m")

    def _strip_ansi(text: str) -> str:
        return ansi_escape.sub("", text or "")

    def _detect_failed_test_library(text: str) -> Optional[str]:
        """Determine which test library failed.

        Returns:
            "pytest" | "playwright" | "vitest" | None
        """
        text = _strip_ansi(text)
        if not text:
            return None

        # pytest failure patterns
        if re.search(r"^FAILED\s+[^\s:]+\.py", text, re.MULTILINE):
            return "pytest"
        if re.search(r"=+ FAILURES =+", text, re.MULTILINE):
            return "pytest"
        if re.search(r"=+ \d+ failed", text, re.MULTILINE):
            return "pytest"
        # pytest traceback lines (tests/ directory .py files)
        if re.search(r"(?:^|\s)(?:tests?/)[^:\s]+\.py:\d+", text, re.MULTILINE):
            return "pytest"

        # Playwright failure patterns
        if re.search(r"^\s*[✘×xX]\s+\d+\s+\[[^\]]+\]\s+›", text, re.MULTILINE):
            return "playwright"
        if re.search(r"^\s*\d+\)\s+\[[^\]]+\]\s+›\s+[^\s:]+\.spec\.ts", text, re.MULTILINE):
            return "playwright"
        if re.search(r"\d+ failed.*playwright", text, re.IGNORECASE):
            return "playwright"

        # Vitest failure patterns
        # Patterns like "FAIL  |unit| src/tests/..."
        # Match even in the middle of lines to handle log output
        if re.search(r"FAIL\s+(?:\|[^|]+\|\s+)?[^\s>]+\.(?:spec|test)\.ts", text):
            return "vitest"
        if re.search(r"Test Files\s+\d+ failed", text, re.MULTILINE):
            return "vitest"

        return None

    def _collect_pytest_candidates(text: str) -> List[str]:
        """Extract pytest failed test files."""
        text = _strip_ansi(text)
        if not text:
            return []

        found: List[str] = []

        # 1) Extract from pytest FAILED summary lines
        for pat in [
            r"^FAILED\s+([^\s:]+\.py)::",
            r"^FAILED\s+([^\s:]+\.py)\s*[-:]",
            r"^FAILED\s+([^\s:]+\.py)\b",
        ]:
            m = re.search(pat, text, re.MULTILINE)
            if m:
                found.append(m.group(1))
                break

        # 2) Extract .py files under tests/ from pytest traceback lines
        m = re.search(r"(^|\s)((?:tests?/|^tests?/)[^:\s]+\.py):\d+", text, re.MULTILINE)
        if m:
            py_path = m.group(2)
            if py_path not in found:
                found.append(py_path)

        return found

    def _collect_playwright_candidates(text: str) -> List[str]:
        """Extract Playwright failed test files."""
        text = _strip_ansi(text)
        if not text:
            return []

        found: List[str] = []
        lines = text.split("\n")

        fail_bullet_re = re.compile(r"^[^\S\r\n]*[✘×xX]\s+\d+\s+\[[^\]]+\]\s+›\s+([^\s:]+\.spec\.ts):\d+:\d+")
        fail_heading_re = re.compile(r"^[^\S\r\n]*\d+\)\s+\[[^\]]+\]\s+›\s+([^\s:]+\.spec\.ts):\d+:\d+")

        def _normalize_spec(path: str) -> str:
            m_e2e = re.search(r"(?:^|/)(e2e/[A-Za-z0-9_./-]+\.spec\.ts)$", path)
            return m_e2e.group(1) if m_e2e else path

        for ln in lines:
            m = fail_bullet_re.search(ln)
            if m:
                norm = _normalize_spec(m.group(1))
                if norm not in found:
                    found.append(norm)

        for ln in lines:
            m = fail_heading_re.search(ln)
            if m:
                norm = _normalize_spec(m.group(1))
                if norm not in found:
                    found.append(norm)

        # Fallback: Search for lines containing .spec.ts
        if not found:
            for spec_path in re.findall(r"([^\s:]+\.spec\.ts)", text):
                norm = _normalize_spec(spec_path)
                if norm not in found:
                    found.append(norm)

        return found

    def _collect_vitest_candidates(text: str) -> List[str]:
        """Extract Vitest failed test files."""
        text = _strip_ansi(text)
        if not text:
            return []

        found: List[str] = []

        # Extract .test.ts / .spec.ts from Vitest/Jest format FAIL lines
        # Match even in the middle of lines to handle log output
        vitest_fail_re = re.compile(
            r"FAIL\s+(?:\|[^|]+\|\s+)?([^\s>]+\.(?:spec|test)\.ts)(?=\s|>|$)",
        )
        for m in vitest_fail_re.finditer(text):
            path = m.group(1)
            if path not in found:
                found.append(path)

        return found

    # Analyze stderr first, then stdout, if neither found, analyze combined output as before
    ordered_outputs = [stderr, stdout, f"{stdout}\n{stderr}"]
    candidates: List[str] = []

    for output in ordered_outputs:
        # Step 1: Determine which test library failed
        failed_library = _detect_failed_test_library(output)

        if failed_library == "pytest":
            candidates = _collect_pytest_candidates(output)
        elif failed_library == "playwright":
            candidates = _collect_playwright_candidates(output)
        elif failed_library == "vitest":
            candidates = _collect_vitest_candidates(output)

        if candidates:
            break

    # Prefer to return existing files
    for path in candidates:
        if os.path.exists(path):
            return path

    # If candidates exist, return the first candidate even if it doesn't exist
    if candidates:
        return candidates[0]

    return None


def log_action(action: str, success: bool = True, details: str = "") -> str:
    """Standardized action logging."""
    message = action
    if details:
        message += f": {details}"

    if success:
        logger.info(message)
    else:
        logger.error(message)
    return message

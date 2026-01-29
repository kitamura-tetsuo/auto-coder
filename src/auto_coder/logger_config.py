"""Logger configuration using loguru.

This module centralizes all logging configuration and formatting for
Auto-Coder.  The default format previously emitted absolute paths coming from
the executing Python environment (e.g. the pipx site-packages directory).  In
practice this produced very long, noisy file paths such as::

    /home/node/.local/pipx/venvs/auto-coder/lib/python3.11/site-packages/auto_coder/utils.py

To keep log output focused on project-relevant information we strip those
environment specific prefixes and report file paths relative to the
``auto_coder`` package root instead.

Environment Variables
---------------------

LLM_LOGGING_DISABLED
    When set to "1", "true", "yes", or "on", disables file logging even when
    a log file path is specified. This can be useful to temporarily disable
    logging without modifying the application configuration. Console logging
    continues to work normally.

    Note: This only affects the main application logging. The LLM output logger
    has its own separate configuration via AUTO_CODER_LLM_OUTPUT_LOG_ENABLED.

Examples
--------

To disable file logging::

    export LLM_LOGGING_DISABLED=1
    auto-coder process-issues --log-file /tmp/app.log  # File logging will be skipped

To re-enable file logging::

    unset LLM_LOGGING_DISABLED
"""

import os
import sys
from functools import wraps
from inspect import iscoroutinefunction, signature
from pathlib import Path
from typing import Any, Callable, Optional, TextIO, Union

from loguru import logger

# Import types for type checking (only used for type hints)
try:
    from loguru import Record

    Record  # to avoid unused import warning
except ImportError:
    pass  # Record will be used as a forward reference

from .config import settings
from .security_utils import redact_string

# Determine the base directory that should be removed from log paths.  When the
# package is installed this resolves to ``.../site-packages``.  When running
# directly from the repository it resolves to ``.../src``.
_PACKAGE_DIR = Path(__file__).resolve().parent
# Include both the src directory and the project root for path trimming
_PROJECT_ROOT = _PACKAGE_DIR.parent.parent.resolve()
_PATH_TRIM_BASES = (_PACKAGE_DIR.parent.resolve(), _PROJECT_ROOT, Path.cwd())


def format_path_for_log(file_path: str) -> str:
    """Return a concise, project-relative path for logging purposes.

    Args:
        file_path: Original absolute file path reported by loguru.

    Returns:
        A trimmed path relative to :data:`_PATH_TRIM_BASES` when possible.  If
        the path is outside our project roots the original path is returned.
    """

    path = Path(file_path)
    try:
        resolved = path.resolve()
    except OSError:
        # Some environments (e.g. zipimport) may not support ``resolve``.
        resolved = path

    for base in _PATH_TRIM_BASES:
        try:
            trimmed = resolved.relative_to(base)
        except ValueError:
            continue
        else:
            return trimmed.as_posix()

    return str(resolved)


def _patch_record(record: dict) -> None:
    """Enrich log records with shortened file paths."""

    record["extra"]["short_path"] = format_path_for_log(record["file"].path)


def setup_logger(
    log_level: Optional[str] = None,
    log_file: Optional[str] = None,
    include_file_info: bool = True,
    stream: Optional[TextIO] = None,
    progress_footer: Any = None,
) -> None:
    """
    Setup loguru logger with file and line information.

    Args:
        log_level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Optional log file path
        include_file_info: Whether to include file and line information in logs
        stream: Stream to write console logs to. Defaults to stderr unless verbose
            logging is requested via AUTOCODER_VERBOSE, in which case stdout is used
            so end-to-end runs can assert on log content.
        progress_footer: Optional ProgressFooter instance to use for sink wrapping

    Raises:
        ValueError: If an invalid log level is provided
    """
    # Remove existing handlers and reset any previous patchers
    logger.remove()
    logger.configure(patcher=None)

    # Use provided log level or fall back to settings
    level = log_level or settings.log_level

    # Validate log level
    valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    if level.upper() not in valid_levels:
        raise ValueError(f"Invalid log level '{level}'. Must be one of: {', '.join(valid_levels)}")

    level = level.upper()

    if include_file_info:
        # Ensure records include shortened file paths for formatting
        logger.configure(patcher=_patch_record)  # type: ignore

    # Format with file and line information (VS Code clickable path:line)
    if include_file_info:
        # Keep the file path segment uncolored so VS Code detects clickable links
        format_string = "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | " "<level>{level: <8}</level> | " "{extra[short_path]}:{line} in <cyan>{function}</cyan> - " "<level>{message}</level>"
    else:
        format_string = "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | " "<level>{level: <8}</level> | " "<cyan>{name}</cyan> - " "<level>{message}</level>"

    # Use non-enqueue mode during pytest to avoid background queue growth
    # Also check for pytest plugins and test runner indicators
    in_test = bool(os.environ.get("PYTEST_CURRENT_TEST")) or bool(os.environ.get("PYTEST_RUNNER")) or bool(os.environ.get("_PYTEST_CURRENT_TEST")) or "pytest" in sys.modules or "unittest" in sys.modules
    use_enqueue = False if in_test else True

    # Prefer caller-provided stream; fall back to stdout when verbose logging is
    # enabled so tests capturing stdout can see the trace, otherwise stderr to
    # keep machine-readable stdout clean.
    selected_stream: TextIO
    if stream is not None:
        selected_stream = stream
    else:
        verbose_requested = os.environ.get("AUTOCODER_VERBOSE", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        selected_stream = sys.stdout if verbose_requested else sys.stderr

    # Add console handler (to specified stream or progress footer sink)
    if progress_footer is not None:
        logger.add(
            progress_footer.sink_wrapper,
            format=format_string,
            level=level,
            colorize=True,
            enqueue=use_enqueue,
            catch=True,  # Catch exceptions during logging to prevent shutdown crashes
        )
    else:
        logger.add(
            selected_stream,
            format=format_string,
            level=level,
            colorize=True,
            enqueue=use_enqueue,
        )

    # Add file handler if specified
    if log_file and not os.environ.get("LLM_LOGGING_DISABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        # Ensure log directory exists
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # Securely create the log file if it doesn't exist, and ensure permissions are restricted
        if not log_path.exists():
            try:
                # Create with restricted permissions (600)
                fd = os.open(log_path, os.O_WRONLY | os.O_CREAT, 0o600)
                os.close(fd)
            except Exception:
                # Fallback to standard creation if low-level open fails
                pass

        # Ensure permissions are 600 (rw-------) even if file already existed
        try:
            os.chmod(log_path, 0o600)
        except Exception:
            pass

        # File format without colors
        if include_file_info:
            file_format = "{time:YYYY-MM-DD HH:mm:ss.SSS} | " "{level: <8} | " "{extra[short_path]}:{line} in {function} - " "{message}"
        else:
            file_format = "{time:YYYY-MM-DD HH:mm:ss.SSS} | " "{level: <8} | " "{name} - " "{message}"

        logger.add(
            log_file,
            format=file_format,
            level=level,
            rotation="10 MB",
            retention="7 days",
            compression="zip",
            enqueue=use_enqueue,
        )


def get_logger(name: str) -> Any:
    """
    Get a logger instance with the specified name.

    Args:
        name: Logger name (typically __name__)

    Returns:
        Logger instance
    """
    return logger.bind(name=name)


def get_gh_logger() -> Any:
    """
    Get a logger instance for GitHub Actions logging.

    Returns:
        Logger instance
    """
    return logger


def _format_args(func: Callable, args: tuple, kwargs: dict, max_len: int = 120) -> str:
    """Build a compact call signature string for logging."""
    bound = signature(func).bind_partial(*args, **kwargs)
    bound.apply_defaults()
    s = ", ".join(f"{k}={redact_string(repr(bound.arguments[k]))}" for k in bound.arguments)
    if len(s) > max_len:
        s = s[:max_len] + "…"
    return s


def log_calls(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator: log the fully qualified name on every call (sync & async)."""
    qualname = getattr(func, "__qualname__", getattr(func, "__name__", "<?>"))
    module = getattr(func, "__module__", "<module>")
    where = f"{module}.{qualname}"

    if iscoroutinefunction(func):

        @wraps(func)
        async def wrapper(*args, **kwargs):  # type: ignore
            logger.opt(depth=1).debug(f"CALL {where}({_format_args(func, args, kwargs)})")
            result = await func(*args, **kwargs)
            logger.opt(depth=1).debug(f"RET  {where} -> {redact_string(repr(result))}")
            return result

        return wrapper
    else:

        @wraps(func)
        def wrapper(*args, **kwargs):  # type: ignore
            logger.opt(depth=1).debug(f"CALL {where}({_format_args(func, args, kwargs)})")
            result = func(*args, **kwargs)
            logger.opt(depth=1).debug(f"RET  {where} -> {redact_string(repr(result))}")
            return result

        return wrapper


# Initialize logger on module import if possible
try:
    setup_logger()
except Exception as e:
    # If logger setup fails, we'll have limited logging but the CLI should still work
    # This handles cases where configuration files or environment variables aren't available
    import warnings

    warnings.warn(f"Logger setup failed: {e}. CLI may work but with limited logging.")
    # We still need to ensure the logger object exists minimally
    # logger is already imported at the module level

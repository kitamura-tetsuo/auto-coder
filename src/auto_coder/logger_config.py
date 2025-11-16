"""Logger configuration using loguru.

This module centralizes all logging configuration and formatting for
Auto-Coder.  The default format previously emitted absolute paths coming from
the executing Python environment (e.g. the pipx site-packages directory).  In
practice this produced very long, noisy file paths such as::

    /home/node/.local/pipx/venvs/auto-coder/lib/python3.11/site-packages/auto_coder/utils.py

To keep log output focused on project-relevant information we strip those
environment specific prefixes and report file paths relative to the
``auto_coder`` package root instead.
"""

import os
import sys
from functools import wraps
from inspect import iscoroutinefunction, signature
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional, TypeVar, Union

from loguru import logger

if TYPE_CHECKING:
    from loguru import Logger

from .config import settings

# Determine the base directory that should be removed from log paths.  When the
# package is installed this resolves to ``.../site-packages``.  When running
# directly from the repository it resolves to ``.../src``.
_PACKAGE_DIR = Path(__file__).resolve().parent
_PATH_TRIM_BASES = (_PACKAGE_DIR.parent.resolve(),)


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


def _patch_record(record: Any) -> None:
    """Enrich log records with shortened file paths."""

    record["extra"]["short_path"] = format_path_for_log(record["file"].path)


def setup_logger(
    log_level: Optional[str] = None,
    log_file: Optional[str] = None,
    include_file_info: bool = True,
    stream: Any = sys.stdout,
    progress_footer: Any = None,
) -> None:
    """
    Setup loguru logger with file and line information.

    Args:
        log_level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Optional log file path
        include_file_info: Whether to include file and line information in logs
        stream: Stream to write console logs to (default: sys.stdout). Use sys.stderr to avoid polluting stdout.
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
        logger.configure(patcher=_patch_record)

    # Format with file and line information (VS Code clickable path:line)
    if include_file_info:
        # Keep the file path segment uncolored so VS Code detects clickable links
        format_string = "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | " "<level>{level: <8}</level> | " "{extra[short_path]}:{line} in <cyan>{function}</cyan> - " "<level>{message}</level>"
    else:
        format_string = "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | " "<level>{level: <8}</level> | " "<cyan>{name}</cyan> - " "<level>{message}</level>"

    # Use non-enqueue mode during pytest to avoid background queue growth
    use_enqueue = False if os.environ.get("PYTEST_CURRENT_TEST") else True

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
            stream,
            format=format_string,
            level=level,
            colorize=True,
            enqueue=use_enqueue,
        )

    # Add file handler if specified
    if log_file:
        # Ensure log directory exists
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

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


def get_logger(name: str) -> "Logger":
    """
    Get a logger instance with the specified name.

    Args:
        name: Logger name (typically __name__)

    Returns:
        Logger instance
    """
    return logger.bind(name=name)


def get_config_logger() -> "Logger":
    """
    Get a logger instance specifically for configuration-related operations.

    Returns:
        Logger instance for configuration logging
    """
    return logger.bind(name="config")


def _format_args(func: Any, args: Any, kwargs: Any, max_len: int = 120) -> str:
    """Build a compact call signature string for logging."""
    bound = signature(func).bind_partial(*args, **kwargs)
    bound.apply_defaults()
    s = ", ".join(f"{k}={bound.arguments[k]!r}" for k in bound.arguments)
    if len(s) > max_len:
        s = s[:max_len] + "…"
    return s


F = TypeVar("F", bound=Callable[..., Any])


def log_calls(func: F) -> F:
    """Decorator: log the fully qualified name on every call (sync & async)."""
    qualname = getattr(func, "__qualname__", getattr(func, "__name__", "<?>"))
    module = getattr(func, "__module__", "<module>")
    where = f"{module}.{qualname}"

    if iscoroutinefunction(func):

        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            logger.opt(depth=1).debug(f"CALL {where}({_format_args(func, args, kwargs)})")
            result = await func(*args, **kwargs)
            logger.opt(depth=1).debug(f"RET  {where} -> {result!r}")
            return result

        return wrapper  # type: ignore
    else:

        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            logger.opt(depth=1).debug(f"CALL {where}({_format_args(func, args, kwargs)})")
            result = func(*args, **kwargs)
            logger.opt(depth=1).debug(f"RET  {where} -> {result!r}")
            return result

        return wrapper  # type: ignore


# Initialize logger on module import
setup_logger()

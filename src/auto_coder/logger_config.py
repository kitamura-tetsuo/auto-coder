"""
Logger configuration using loguru.
"""

import sys
from pathlib import Path
from loguru import logger
from typing import Optional

from .config import settings


def setup_logger(
    log_level: Optional[str] = None,
    log_file: Optional[str] = None,
    include_file_info: bool = True
) -> None:
    """
    Setup loguru logger with file and line information.
    
    Args:
        log_level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Optional log file path
        include_file_info: Whether to include file and line information in logs
    
    Raises:
        ValueError: If an invalid log level is provided
    """
    # Remove default handler
    logger.remove()
    
    # Use provided log level or fall back to settings
    level = log_level or settings.log_level
    
    # Validate log level
    valid_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
    if level.upper() not in valid_levels:
        raise ValueError(f"Invalid log level '{level}'. Must be one of: {', '.join(valid_levels)}")
    
    level = level.upper()
    
    # Format with file and line information
    if include_file_info:
        format_string = (
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        )
    else:
        format_string = (
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan> - "
            "<level>{message}</level>"
        )
    
    # Add console handler
    logger.add(
        sys.stdout,
        format=format_string,
        level=level,
        colorize=True,
        enqueue=True
    )
    
    # Add file handler if specified
    if log_file:
        # Ensure log directory exists
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        
        # File format without colors
        file_format = (
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
            "{level: <8} | "
            "{name}:{function}:{line} - "
            "{message}"
        )
        
        logger.add(
            log_file,
            format=file_format,
            level=level,
            rotation="10 MB",
            retention="7 days",
            compression="zip",
            enqueue=True
        )


def get_logger(name: str):
    """
    Get a logger instance with the specified name.
    
    Args:
        name: Logger name (typically __name__)
        
    Returns:
        Logger instance
    """
    return logger.bind(name=name)


# Initialize logger on module import
setup_logger()

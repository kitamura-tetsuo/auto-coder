"""ログ設定モジュール."""

import sys

from loguru import logger


def setup_logger(verbose: bool = False) -> None:
    """ロガーを設定する.

    Args:
        verbose: Whether to enable verbose logging
    """
    # デフォルトのハンドラーを削除
    logger.remove()

    # コンソール出力を追加
    log_level = "DEBUG" if verbose else "INFO"
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level=log_level,
        colorize=True,
    )


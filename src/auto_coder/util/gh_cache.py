"""
This module provides a caching mechanism for httpx clients using hishel.
"""

import hishel
import httpx

from ..config import settings


def get_caching_client() -> hishel.CacheClient:
    """
    Returns a caching httpx client configured with hishel.
    """
    if settings.hishel_cache_path:
        storage = hishel.FileStorage(base_path=settings.hishel_cache_path)
    else:
        storage = hishel.InMemoryStorage()

    return hishel.CacheClient(storage=storage)

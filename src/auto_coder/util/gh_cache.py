
import threading

import httpx
from hishel import SyncSqliteStorage
from hishel.httpx import SyncCacheClient

_storage_instance = None
_storage_lock = threading.Lock()


def get_caching_client() -> httpx.Client:
    """
    Returns a singleton instance of a caching httpx client using hishel.
    """
    global _storage_instance
    if _storage_instance is None:
        with _storage_lock:
            if _storage_instance is None:
                _storage_instance = SyncSqliteStorage(database_path=".cache/gh_cache.db")
    return SyncCacheClient(storage=_storage_instance)

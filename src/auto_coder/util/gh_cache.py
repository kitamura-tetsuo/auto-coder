import httpx
from hishel import SyncSqliteStorage
from hishel.httpx import SyncCacheClient


def get_caching_client() -> httpx.Client:
    """
    Returns a caching httpx client using hishel.
    """
    return SyncCacheClient(storage=SyncSqliteStorage(database_path=".cache/gh_cache.db"))

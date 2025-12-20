import httpx
from hishel import SyncSqliteStorage
from hishel.httpx import SyncCacheTransport


def get_caching_transport():
    """Returns a configured hishel.CacheTransport."""
    storage = SyncSqliteStorage(database_path=".cache/gh/cache.sqlite")
    transport = httpx.HTTPTransport()
    cache_transport = SyncCacheTransport(next_transport=transport, storage=storage)
    return cache_transport

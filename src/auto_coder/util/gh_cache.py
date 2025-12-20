import httpx
from hishel.httpx import SyncCacheClient
from hishel.storages import FileStorage

def get_caching_client() -> httpx.Client:
    """
    Returns a caching httpx client using hishel.
    """
    return SyncCacheClient(storage=FileStorage(base_path=".cache/gh"))

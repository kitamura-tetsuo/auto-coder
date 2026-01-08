import threading

import httpx
from ghapi.all import GhApi
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


def get_ghapi_client(token: str, **kwargs) -> GhApi:
    """
    Returns a GhApi instance configured with hishel caching for GET requests.
    """

    # Adapter implementation for automatic ETag handling
    def httpx_adapter(self, path, verb, headers, route, query, data):
        client = get_caching_client()
        url = f"{self.endpoint}{path}"
        
        # Ensure query is a dict
        params = query if query else {}

        resp = client.request(
            method=verb,
            url=url,
            headers=headers,
            content=data,
            params=params,
            # Force cache usage for GET requests
            extensions={"force_cache": True} if verb.upper() == "GET" else {}
        )

        self.last_headers = dict(resp.headers)

        # Handle non-JSON (e.g. diffs) or empty responses
        if resp.status_code == 204 or not resp.text:
            return None

        try:
            return resp.json()
        except Exception:
            return resp.text

    api = GhApi(token=token, **kwargs)
    api._call = httpx_adapter.__get__(api, GhApi)
    return api


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


def get_ghapi_client(token: str) -> GhApi:
    """
    Returns a GhApi instance configured with hishel caching for GET requests.
    """

    # Adapter implementation for automatic ETag handling
    def httpx_adapter(self, path, verb, headers, route, query, data):
        # Use the shared caching client to benefit from shared storage
        # logic but we need a fresh client or re-use existing one?
        # The user example creates a new CacheClient every time which is inefficient.
        # We should stick to the user's proposed adapter logic but maybe optimize it
        # or use our existing get_caching_client() if possible.
        # However, fastai/ghapi expects a specific signature.

        # Re-using the logic from the user proposal but pointing to our cache location
        # and using our shared client if possible, OR just following the pattern.
        # The user proposal:
        # cache_client = hishel.CacheClient(storage=hishel.FileStorage(base_path='.cache/gh'))
        #
        # Our get_caching_client() returns a SyncCacheClient with SqliteStorage.
        # Let's use that.

        client = get_caching_client()

        # GhApi constructs the full URL itself? No, self.endpoint is base.
        # implementation details of GhApi:
        # url = f'{self.endpoint}{path}'
        # But GhApi._call might pass path as full url sometimes?
        # Let's follow the user's snippet which does: url = f'{self.endpoint}{path}'

        url = f"{self.endpoint}{path}"

        # ghapi passes headers, data, etc.
        # We need to map 'verb' to request method.
        # verb is like 'GET', 'POST', etc.

        # IMPORTANT: Only cache GET requests (hishel handles this check internally usually,
        # but good to be explicit or let hishel do it).
        # user snippet:
        # resp = cache_client.request(method=verb, url=url, headers=headers, content=data)

        # We need to handle query params too.
        # ghapi passes 'query' (dict). httpx takes 'params'.

        # The user snippet ignored 'query'. We must include it.

        resp = client.request(
            method=verb,
            url=url,
            headers=headers,
            content=data,
            params=query,
        )

        # Update last headers
        self.last_headers = dict(resp.headers)

        # ghapi expects parsed JSON or None
        if resp.status_code == 204 or not resp.text:
            return None

        # Handle errors (raise if not successful?)
        # GhApi usually expects raw response or json.
        # If we return json, GhApi is happy.
        # But we should probably raise for status if it's an error,
        # because GhApi does that in its own requests integration.
        if resp.is_error:
            resp.raise_for_status()

        return resp.json()

    api = GhApi(token=token)
    # Monkey patch the _call method as requested
    api._call = httpx_adapter.__get__(api, GhApi)
    return api

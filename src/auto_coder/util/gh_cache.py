import threading

import hishel

_cache_client = None
_lock = threading.Lock()


def get_cache_client():
    """Returns a singleton instance of the hishel.CacheClient."""
    global _cache_client
    if _cache_client is None:
        with _lock:
            if _cache_client is None:
                storage = hishel.FileStorage(base_path=".cache/gh")
                _cache_client = hishel.CacheClient(storage=storage)
    return _cache_client


def httpx_adapter(self, path, verb, headers, route, query, data):
    cache_client = get_cache_client()
    url = f"{self.endpoint}{path}"
    resp = cache_client.request(method=verb, url=url, headers=headers, content=data)
    self.last_headers = dict(resp.headers)
    return resp.json() if resp.text else None

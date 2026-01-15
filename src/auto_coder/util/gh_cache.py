import logging
import threading

import httpx
from ghapi.all import GhApi
from hishel import SyncSqliteStorage
from hishel.httpx import SyncCacheClient

logger = logging.getLogger(__name__)

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

    class CachedGhApi(GhApi):
        def __call__(self, path: str, verb: str = None, headers: dict = None, route: dict = None, query: dict = None, data=None, timeout=None, decode=True):
            # Use the shared caching client
            client = get_caching_client()

            if verb is None:
                verb = "POST" if data else "GET"

            # Build URL
            # GhApi uses .gh_host but we must be careful if path is absolute
            if path.startswith("http"):
                url = path
            else:
                url = f"{self.gh_host}{path}"

            # Merge headers
            headers = {**self.headers, **(headers or {})}

            # Handle route params in path (GhApi does this but we might need to do it if we are replacing logic)
            # Actually GhApi.__call__ does:
            # if route: for k,v in route.items(): route[k] = quote(str(route[k]), safe='')
            # But we are passing `route` to `httpx`? No, `httpx` doesn't take `route`.
            # We need to format the path?
            # GhApi passes `route` to `urlsend`.
            # `urlsend` likely formats the URL using route params?
            # Let's check `urlsend` signature if possible.
            # But usually `route` params are for template strings in path.
            # If `path` has `{owner}`, then `route` has `{'owner': ...}`.

            if route:
                import urllib.parse

                for k, v in route.items():
                    # value quoting
                    v_str = urllib.parse.quote(str(v), safe="")
                    path = path.replace(f"{{{k}}}", v_str)
                # Re-evaluate URL after path interpolation
                if not path.startswith("http"):
                    url = f"{self.gh_host}{path}"
                else:
                    url = path

            # Handle data arg for httpx (json vs content)
            json_data = None
            content_data = None
            if data is not None:
                if isinstance(data, dict):
                    json_data = data
                else:
                    content_data = data

            # Use params=query for GET params
            # httpx handles redirects and strips Authorization on cross-origin

            resp = client.request(method=verb, url=url, headers=headers, content=content_data, json=json_data, params=query, follow_redirects=True, timeout=timeout)

            # Attach history
            # resp.history is automatically populated by httpx

            # Update last headers
            try:
                self.recv_hdrs = dict(resp.headers)
            except:
                pass

            # ghapi expects parsed JSON or None
            if resp.status_code == 204 or (not resp.text and not resp.content):
                return None

            # Handle return_json behavior based on headers or decode arg?
            # GhApi __call__: return_json = ('json' in headers['Accept']) and (decode is True)
            # Default Accept is usually json?

            # Use GhApi-like return logic
            content_type = resp.headers.get("content-type", "")

            if decode:
                if "application/zip" in content_type or "application/octet-stream" in content_type:
                    return resp.content

                try:
                    return resp.json()
                except Exception:
                    pass
                return resp.text

            return resp

    return CachedGhApi(token=token)


from src.auto_coder.util.gh_cache import get_ghapi_client
import sys

try:
    api = get_ghapi_client("dummy_token")
    print(f"api.issues type: {type(api.issues)}")
    print(f"Has get_parent_issue: {hasattr(api.issues, 'get_parent_issue')}")
    # inspect what IS available
    # print(dir(api.issues)) 
except Exception as e:
    print(e)

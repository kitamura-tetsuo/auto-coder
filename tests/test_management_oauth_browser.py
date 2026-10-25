"""Real-browser regression coverage for the shipped management consent page."""

import base64
import hashlib
import socket
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
import uvicorn
from fastapi.responses import HTMLResponse

from auto_coder.management.oauth_config import OAuthClient, OAuthSettings
from auto_coder.management.oauth_grants import OAuthGrantService
from auto_coder.management.oauth_http import create_management_auth_app
from tests.support.browser_launch import headless_page
from tests.test_management_identity_sdk import sdk_boundary  # noqa: F401

pytestmark = pytest.mark.browser


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def test_shipped_page_hands_signed_identity_to_real_verifier_and_requires_consent(tmp_path, sdk_boundary, _use_real_sleep):
    identity, signed_token, _state, _policy, _now = sdk_boundary
    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    callback = f"{origin}/callback"
    settings = OAuthSettings(
        tmp_path / "browser.db",
        origin,
        "test-project",
        (OAuthClient("browser-client", (callback,)),),
        firebase_web_config={"apiKey": "public", "authDomain": "test-project.firebaseapp.com", "projectId": "test-project"},
    )
    grants = OAuthGrantService(settings, identity)
    app = create_management_auth_app(settings, grants)

    @app.get("/callback")
    async def callback_page():
        return HTMLResponse("callback reached")

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started

    params = urlencode(
        {
            "response_type": "code",
            "client_id": "browser-client",
            "redirect_uri": callback,
            "resource": settings.resource,
            "scope": "auto-coder.read",
            "state": "browser-state",
            "code_challenge": _challenge("v" * 43),
            "code_challenge_method": "S256",
        }
    )
    try:
        with headless_page(viewport={"width": 1024, "height": 768}) as page:
            page.route(
                "https://www.gstatic.com/firebasejs/10.14.1/firebase-app.js",
                lambda route: route.fulfill(content_type="text/javascript", body="export function initializeApp(config) { return config; }"),
            )
            token_literal = signed_token().replace("\\", "\\\\").replace("'", "\\'")
            page.route(
                "https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js",
                lambda route: route.fulfill(
                    content_type="text/javascript",
                    body=("export function getAuth(app) { return app; }\n" "export class GoogleAuthProvider {}\n" f"export async function signInWithPopup() {{ return {{user: {{getIdToken: async () => '{token_literal}'}}}}; }}"),
                ),
            )
            page.goto(f"{origin}/authorize?{params}")
            assert page.locator("#client").text_content() == "browser-client"
            assert page.locator("#resource").text_content() == settings.resource
            assert page.locator("#allow").is_disabled()
            with grants.store.connect() as db:
                assert db.execute("SELECT COUNT(*) FROM codes").fetchone()[0] == 0

            page.locator("#signin").click()
            page.locator("#allow").wait_for(state="visible")
            assert not page.locator("#allow").is_disabled()
            page.locator("#allow").click()
            page.wait_for_url(f"{callback}*")
            query = parse_qs(urlsplit(page.url).query)
            assert query["state"] == ["browser-state"]
            assert query["iss"] == [origin]
            assert len(query["code"]) == 1

            page.goto(f"{origin}/authorize?{params.replace('browser-state', 'deny-state')}")
            assert page.locator("#allow").is_disabled()
            page.locator("#deny").click()
            page.wait_for_url(f"{callback}*")
            denied = parse_qs(urlsplit(page.url).query)
            assert denied == {"iss": [origin], "state": ["deny-state"], "error": ["access_denied"]}
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()

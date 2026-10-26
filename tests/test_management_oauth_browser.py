"""Real-browser regression coverage for the shipped management consent page."""

import base64
import hashlib
import socket
import threading
import time
import urllib.request
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
import uvicorn
from fastapi.responses import HTMLResponse
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

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
    csp_mutation = {"enabled": False}

    @app.middleware("http")
    async def mutate_consent_csp(request, call_next):
        response = await call_next(request)
        if csp_mutation["enabled"] and request.url.path.startswith("/consent/"):
            response.headers["content-security-policy"] = response.headers["content-security-policy"].replace(" https://*.firebaseapp.com", "")
        return response

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
    firebase_module_urls = {name: f"https://www.gstatic.com/firebasejs/10.14.1/{name}" for name in ("firebase-app.js", "firebase-auth.js")}
    actual_firebase_modules = {url: urllib.request.urlopen(url, timeout=30).read() for url in firebase_module_urls.values()}
    actual_gapi = urllib.request.urlopen("https://apis.google.com/js/api.js", timeout=30).read().decode()
    assert all(len(source) > 10_000 for source in actual_firebase_modules.values())
    assert len(actual_gapi) > 10_000
    try:
        with headless_page(viewport={"width": 1024, "height": 768}) as page:
            loaded_firebase_modules = []
            loaded_auth_iframes = []
            requested_urls = []
            browser_errors = []
            page.on("request", lambda request: requested_urls.append(request.url))
            page.on("console", lambda message: browser_errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: browser_errors.append(str(error)))

            def load_real_firebase_module(route):
                loaded_firebase_modules.append(route.request.url)
                route.fulfill(
                    status=200,
                    content_type="text/javascript",
                    headers={"Access-Control-Allow-Origin": "*"},
                    body=actual_firebase_modules[route.request.url],
                )

            page.context.route("https://www.gstatic.com/firebasejs/10.14.1/firebase-*.js", load_real_firebase_module)
            page.context.route(
                "https://apis.google.com/js/api.js*",
                lambda route: route.fulfill(
                    content_type="text/javascript",
                    body=actual_gapi,
                ),
            )

            controlled_provider_transport = """
window.__actualGapiLoaded = typeof window.gapi?.load === 'function';
window.gapi.iframes = {
  Iframe: function () {}, CROSS_ORIGIN_IFRAMES_FILTER: {},
  getContext: () => ({open: (options, ready) => new Promise((resolve, reject) => {
    const element = document.createElement('iframe');
    element.dataset.controlledProvider = 'true';
    element.src = options.url;
    const providerReady = event => {
      if (event.source !== element.contentWindow || !event.data?.controlledProviderReady) return;
      window.removeEventListener('message', providerReady);
      const iframe = {
        restyle: async () => {}, ping: done => { done(); return Promise.resolve(); },
        register: (name, handler) => window.addEventListener('message', event => {
          if (event.data?.event === name) handler(event.data.payload);
        }),
        send: () => {}
      };
      ready(iframe).then(resolve, reject);
    };
    window.addEventListener('message', providerReady);
    document.body.appendChild(element);
  })})
};
"""

            def firebase_api(route):
                if "/v1/projects" in route.request.url:
                    route.fulfill(status=200, content_type="application/json", body='{"authorizedDomains":["127.0.0.1"]}')
                elif "accounts:lookup" in route.request.url:
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body='{"users":[{"localId":"operator","email":"operator@example.test","providerUserInfo":[]}]}',
                    )
                else:
                    token = signed_token()
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=('{"localId":"operator","email":"operator@example.test",' f'"idToken":"{token}","refreshToken":"controlled-refresh",' '"expiresIn":"3600","providerId":"google.com",' '"rawUserInfo":"{\\"sub\\":\\"operator\\"}"}'),
                    )

            page.context.route("https://identitytoolkit.googleapis.com/**", firebase_api)

            def firebase_auth_domain(route):
                if "/iframe" in route.request.url:
                    loaded_auth_iframes.append(route.request.url)
                    route.fulfill(
                        content_type="text/html",
                        body="<script>parent.postMessage({controlledProviderReady:true}, '*')</script>",
                    )
                else:
                    route.fulfill(content_type="text/html", body="controlled provider popup")

            page.context.route("https://test-project.firebaseapp.com/__/auth/**", firebase_auth_domain)
            page.goto(f"{origin}/authorize?{params}")
            page.wait_for_function("document.getElementById('signin').onclick !== null")
            page.add_script_tag(url="https://apis.google.com/js/api.js")
            page.evaluate(controlled_provider_transport)
            assert page.evaluate("window.__actualGapiLoaded") is True
            assert page.locator("#client").text_content() == "browser-client"
            assert page.locator("#resource").text_content() == settings.resource
            assert page.locator("#allow").is_disabled()
            with grants.store.connect() as db:
                assert db.execute("SELECT COUNT(*) FROM codes").fetchone()[0] == 0

            try:
                with page.expect_popup(timeout=5000) as popup_info:
                    page.locator("#signin").click()
            except PlaywrightTimeoutError as exc:
                raise AssertionError(f"Firebase popup did not open: {page.locator('#status').text_content()}; " f"errors={browser_errors}; requests={requested_urls}") from exc
            popup = popup_info.value
            try:
                popup.wait_for_url("https://test-project.firebaseapp.com/**", wait_until="commit", timeout=5000)
            except PlaywrightTimeoutError as exc:
                raise AssertionError(f"Firebase popup used unexpected URL {popup.url}; requests={requested_urls}") from exc
            event_id = parse_qs(urlsplit(popup.url).query)["eventId"][0]
            assert page.locator("iframe[data-controlled-provider='true']").count() == 1
            assert any("test-project.firebaseapp.com/__/auth/iframe" in url for url in requested_urls)
            assert len(loaded_auth_iframes) == 1
            popup.evaluate(
                """eventId => window.opener.postMessage({event: 'authEvent', payload: {authEvent: {
                    type: 'signInViaPopup', eventId,
                    urlResponse: 'http://127.0.0.1/provider-callback',
                    sessionId: 'controlled-session', postBody: 'providerId=google.com',
                    tenantId: null
                }}}, '*')""",
                event_id,
            )
            deadline = time.monotonic() + 5
            while page.locator("#allow").is_disabled() and time.monotonic() < deadline:
                page.wait_for_timeout(25)
            if page.locator("#allow").is_disabled():
                raise AssertionError(f"Firebase handoff did not enable consent: {page.locator('#status').text_content()}; " f"errors={browser_errors}; requests={requested_urls}")
            assert {url.rsplit("/", 1)[-1] for url in loaded_firebase_modules} == {"firebase-app.js", "firebase-auth.js"}
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

            # A CSP regression must stop the real browser handoff before Firebase
            # can open the provider popup or submit identity evidence.  The
            # controlled auth-domain document acknowledges readiness through the
            # iframe's actual WindowProxy, so a blocked frame cannot be mistaken
            # for a successful SDK initialization.
            csp_mutation["enabled"] = True
            requests_before_blocked_flow = len(requested_urls)
            blocked_page = page.goto(f"{origin}/authorize?{params.replace('browser-state', 'blocked-frame-state')}")
            assert blocked_page is not None
            assert "firebaseapp.com" not in blocked_page.headers["content-security-policy"]
            page.wait_for_function("document.getElementById('signin').onclick !== null")
            page.add_script_tag(url="https://apis.google.com/js/api.js")
            page.evaluate(controlled_provider_transport)
            page.locator("#signin").click()
            page.wait_for_timeout(500)
            blocked_requests = requested_urls[requests_before_blocked_flow:]
            assert page.locator("#allow").is_disabled()
            assert not any("test-project.firebaseapp.com/__/auth/iframe" in url for url in blocked_requests)
            with grants.store.connect() as db:
                assert db.execute("SELECT COUNT(*) FROM codes").fetchone()[0] == 1
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()

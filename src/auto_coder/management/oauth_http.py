"""HTTP and browser adapter for the management OAuth grant authority.

The durable service remains the only credential authority.  This module only
validates protocol input, binds a browser transaction, and translates results
to OAuth HTTP responses.
"""

import json
import secrets
from contextlib import asynccontextmanager
from importlib.resources import files
from typing import Any, AsyncContextManager, Callable, Mapping
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from mcp.server.auth.routes import create_auth_routes  # official SDK route primitive
from starlette.concurrency import run_in_threadpool

from .oauth_config import SUPPORTED_SCOPE, OAuthSettings
from .oauth_grants import (
    AuthorizationRequest,
    InvalidClient,
    InvalidGrant,
    InvalidRequest,
    InvalidScope,
    InvalidTarget,
    OAuthError,
    OAuthGrantService,
    Unavailable,
)

_SECURITY_QUERY_FIELDS = {
    "client_id",
    "redirect_uri",
    "response_type",
    "resource",
    "scope",
    "state",
    "code_challenge",
    "code_challenge_method",
}
_SECURITY_FORM_FIELDS = {
    "grant_type",
    "client_id",
    "code",
    "redirect_uri",
    "resource",
    "code_verifier",
    "refresh_token",
    "scope",
    "token",
}


def _oauth_error(code: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status, headers={"Cache-Control": "no-store"})


def _single(values: Mapping[str, list[str]], names: set[str]) -> bool:
    return all(len(values.get(name, [])) <= 1 for name in names)


def _scope(raw: str | None) -> tuple[str, ...] | None:
    if raw is None:
        return None
    if not raw or raw.split() != [SUPPORTED_SCOPE]:
        raise InvalidScope()
    return (SUPPORTED_SCOPE,)


def _redirect(result: object, *, denied: bool = False) -> RedirectResponse:
    redirect_uri = getattr(result, "redirect_uri")
    query: dict[str, str] = {"iss": getattr(result, "issuer")}
    state = getattr(result, "state")
    if state is not None:
        query["state"] = state
    if denied:
        query["error"] = "access_denied"
    else:
        query["code"] = getattr(result, "code")
    separator = "&" if urlsplit(redirect_uri).query else "?"
    return RedirectResponse(redirect_uri + separator + urlencode(query), status_code=303, headers={"Cache-Control": "no-store"})


def _cookie_name(settings: OAuthSettings, request_id: str) -> str:
    prefix = "__Host-" if settings.canonical_issuer.startswith("https://") else ""
    return f"{prefix}ac_oauth_{request_id}"


def _origin_ok(request: Request, settings: OAuthSettings) -> bool:
    return request.headers.get("origin") == settings.canonical_issuer


def create_management_auth_app(
    settings: OAuthSettings,
    grants: OAuthGrantService,
    *,
    lifespan: Callable[[FastAPI], AsyncContextManager[None]] | None = None,
) -> FastAPI:
    """Create the standalone authorization application without owning *grants*.

    ``create_auth_routes`` is deliberately imported and exposed by this adapter as
    the SDK protocol primitive.  The binding-sensitive endpoints are narrow HTTP
    adapters because SDK 1.30 discards the raw resource/verifier before its final
    provider call and cannot implement this service's atomic transitions.
    """

    if lifespan is None:

        @asynccontextmanager
        async def empty_lifespan(_: FastAPI):
            yield

        selected_lifespan: Any = empty_lifespan
    else:
        selected_lifespan = lifespan
    app = FastAPI(lifespan=selected_lifespan)
    app.state.oauth_sdk_route_factory = create_auth_routes

    @app.get("/.well-known/oauth-authorization-server")
    async def metadata() -> JSONResponse:
        issuer = settings.canonical_issuer
        return JSONResponse(
            {
                "issuer": issuer,
                "authorization_endpoint": f"{issuer}/authorize",
                "token_endpoint": f"{issuer}/token",
                "revocation_endpoint": f"{issuer}/revoke",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "scopes_supported": [SUPPORTED_SCOPE],
                "token_endpoint_auth_methods_supported": ["none"],
                "revocation_endpoint_auth_methods_supported": ["none"],
                "authorization_response_iss_parameter_supported": True,
            }
        )

    @app.get("/authorize")
    async def authorize(request: Request) -> Response:
        query: dict[str, list[str]] = {}
        for key, item_value in request.query_params.multi_items():
            query.setdefault(key, []).append(item_value)
        if not _single(query, _SECURITY_QUERY_FIELDS):
            return _oauth_error("invalid_request")

        def query_value(name: str) -> str | None:
            return query.get(name, [None])[0]

        client_id, redirect_uri = query_value("client_id"), query_value("redirect_uri")
        client = settings.client(client_id or "")
        # Never redirect until the client and exact callback have been validated.
        if client is None or redirect_uri not in client.redirect_uris:
            return _oauth_error("invalid_client" if client is None else "invalid_request")
        assert client_id is not None and redirect_uri is not None
        try:
            if query_value("response_type") != "code" or query_value("resource") != settings.resource:
                raise InvalidRequest()
            if query_value("code_challenge_method") != "S256" or not query_value("code_challenge"):
                raise InvalidRequest()
            scopes = _scope(query_value("scope"))
            browser, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            pending = await run_in_threadpool(
                grants.begin_authorization,
                AuthorizationRequest(
                    client_id,
                    redirect_uri,
                    settings.resource,
                    query_value("code_challenge") or "",
                    scopes,
                    query_value("state"),
                ),
                browser,
                csrf,
            )
        except OAuthError as exc:
            return _oauth_error(exc.code, 503 if isinstance(exc, Unavailable) else 400)
        template = files("auto_coder.management.static").joinpath("authorize.html").read_text()
        data = json.dumps(
            {
                "requestId": pending.request_id,
                "csrf": csrf,
                "clientId": client_id,
                "resource": settings.resource,
                "scope": SUPPORTED_SCOPE,
                "firebase": settings.firebase_web_config,
            }
        ).replace("<", "\\u003c")
        body = template.replace("__AUTH_DATA__", data)
        response = HTMLResponse(
            body,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'self'; script-src 'self' https://www.gstatic.com; connect-src 'self' https://*.googleapis.com; frame-src https://*.firebaseapp.com https://accounts.google.com; style-src 'self'; object-src 'none'; base-uri 'none'",
                "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
            },
        )
        response.set_cookie(_cookie_name(settings, pending.request_id), f"{browser}.{csrf}", secure=settings.canonical_issuer.startswith("https://"), httponly=True, samesite="lax", max_age=600, path="/")
        return response

    @app.get("/assets/authorize.js")
    async def authorize_asset() -> Response:
        return Response(files("auto_coder.management.static").joinpath("authorize.js").read_text(), media_type="text/javascript")

    @app.post("/authorize/{request_id}/{action}")
    async def decide(request_id: str, action: str, request: Request) -> Response:
        if action not in {"allow", "deny"} or not _origin_ok(request, settings):
            return _oauth_error("invalid_request")
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            return _oauth_error("invalid_request")
        cookie = request.cookies.get(_cookie_name(settings, request_id), "")
        try:
            browser, cookie_csrf = cookie.split(".", 1)
        except ValueError:
            return _oauth_error("invalid_request")
        supplied_csrf = body.get("csrf")
        if not isinstance(supplied_csrf, str) or not secrets.compare_digest(cookie_csrf, supplied_csrf):
            return _oauth_error("invalid_request")
        try:
            if action == "deny":
                result = await run_in_threadpool(grants.deny_authorization, request_id, browser, supplied_csrf)
                response = _redirect(result, denied=True)
            else:
                token = body.get("firebaseIdToken")
                if not isinstance(token, str) or not token:
                    raise InvalidRequest()
                result = await run_in_threadpool(grants.approve_authorization, request_id, browser, supplied_csrf, token)
                response = _redirect(result)
        except OAuthError as exc:
            return _oauth_error(exc.code, 503 if isinstance(exc, Unavailable) else 400)
        response.delete_cookie(_cookie_name(settings, request_id), path="/")
        return response

    async def form_values(request: Request) -> tuple[dict[str, list[str]], Response | None]:
        form = await request.form()
        values: dict[str, list[str]] = {}
        for key, value in form.multi_items():
            values.setdefault(key, []).append(str(value))
        if not _single(values, _SECURITY_FORM_FIELDS):
            return values, _oauth_error("invalid_request")
        return values, None

    @app.post("/token")
    async def token(request: Request) -> Response:
        values, error = await form_values(request)
        if error:
            return error
        get = lambda name: values.get(name, [None])[0]
        client_id, resource, grant_type = get("client_id"), get("resource"), get("grant_type")
        if not client_id or resource != settings.resource:
            return _oauth_error("invalid_target" if resource != settings.resource else "invalid_client")
        try:
            if grant_type == "authorization_code":
                if get("scope") is not None:
                    _scope(get("scope"))
                bundle = await run_in_threadpool(grants.exchange_code, client_id, get("code") or "", get("redirect_uri") or "", resource, get("code_verifier") or "")
            elif grant_type == "refresh_token":
                scopes = _scope(get("scope"))
                bundle = await run_in_threadpool(grants.refresh, client_id, get("refresh_token") or "", resource, scopes)
            else:
                return _oauth_error("unsupported_grant_type")
        except OAuthError as exc:
            return _oauth_error(exc.code, 503 if isinstance(exc, Unavailable) else 400)
        return JSONResponse({"access_token": bundle.access_token, "refresh_token": bundle.refresh_token, "token_type": bundle.token_type, "expires_in": bundle.expires_in, "scope": bundle.scope}, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    @app.post("/revoke")
    async def revoke(request: Request) -> Response:
        values, error = await form_values(request)
        if error:
            return error
        get = lambda name: values.get(name, [None])[0]
        try:
            await run_in_threadpool(grants.revoke, get("client_id") or "", get("token") or "")
        except Unavailable:
            return _oauth_error("temporarily_unavailable", 503)
        except (InvalidClient, InvalidGrant, InvalidRequest, InvalidScope, InvalidTarget):
            pass
        return Response(status_code=200, headers={"Cache-Control": "no-store"})

    return app

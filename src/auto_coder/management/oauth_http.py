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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.routes import create_auth_routes  # official SDK route primitive
from pydantic import AnyHttpUrl, TypeAdapter
from starlette.concurrency import run_in_threadpool

from .action_scopes import validate_scope_set
from .oauth_config import OAuthSettings
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
from .oauth_provider import CodeExchangeContext, ExchangeContext, ManagementOAuthProvider, RawAuthorizationContext, RefreshExchangeContext, authorization_unavailable, browser_authorization, exchange_context, raw_authorization

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
    try:
        return validate_scope_set(tuple(raw.split()))
    except ValueError as exc:
        raise InvalidScope() from exc


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
    provider = ManagementOAuthProvider(settings, grants)
    # Construct the official route set as an assertion that this adapter remains
    # compatible with the locked SDK surface. Binding-sensitive wrappers below use
    # the same official handlers while retaining raw request fields the SDK drops.
    issuer_url = TypeAdapter(AnyHttpUrl).validate_python(settings.canonical_issuer)
    app.state.oauth_sdk_routes = create_auth_routes(provider, issuer_url)
    token_handler = TokenHandler(provider, ClientAuthenticator(provider))
    authorization_handler = AuthorizationHandler(provider)

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
                "scopes_supported": list(settings.enabled_scopes),
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
        if query_value("response_type") != "code" or query_value("resource") != settings.resource:
            return _oauth_error("invalid_request")
        if query_value("code_challenge_method") != "S256" or not query_value("code_challenge"):
            return _oauth_error("invalid_request")
        try:
            _scope(query_value("scope"))
        except OAuthError as exc:
            return _oauth_error(exc.code)
        browser_token = browser_authorization.set(None)
        raw_token = raw_authorization.set(RawAuthorizationContext(client_id, redirect_uri))
        unavailable_token = authorization_unavailable.set(False)
        try:
            response = await authorization_handler.handle(request)
            if authorization_unavailable.get():
                return _oauth_error("temporarily_unavailable", 503)
            pending = browser_authorization.get()
            if pending is not None:
                response.set_cookie(_cookie_name(settings, pending.request_id), f"{pending.browser_secret}.{pending.csrf_secret}", secure=settings.canonical_issuer.startswith("https://"), httponly=True, samesite="lax", max_age=600, path="/")
            elif 300 <= response.status_code < 400 and response.headers.get("location"):
                # The SDK constructs authorization-error redirects after this
                # adapter has validated the exact registered callback, but does
                # not emit RFC 9207's issuer parameter. Add only that required
                # parameter without normalizing the registered callback.
                location = response.headers["location"]
                split = urlsplit(location)
                callback = urlsplit(redirect_uri)
                protocol_fields = {"code", "error", "error_description", "error_uri", "iss", "state"}
                parameters = [(name, value) for name, value in parse_qsl(callback.query, keep_blank_values=True) if name not in protocol_fields]
                parameters.extend((name, value) for name, value in parse_qsl(split.query, keep_blank_values=True) if name != "iss")
                parameters.append(("iss", settings.canonical_issuer))
                response.headers["location"] = urlunsplit((callback.scheme, callback.netloc, callback.path, urlencode(parameters), callback.fragment))
                response.headers["Cache-Control"] = "no-store"
            return response
        finally:
            authorization_unavailable.reset(unavailable_token)
            raw_authorization.reset(raw_token)
            browser_authorization.reset(browser_token)

    @app.get("/consent/{request_id}")
    async def consent(request_id: str, request: Request) -> Response:
        cookie = request.cookies.get(_cookie_name(settings, request_id), "")
        try:
            browser, csrf = cookie.split(".", 1)
        except ValueError:
            return _oauth_error("invalid_request")
        try:
            pending = await run_in_threadpool(grants.inspect_pending, request_id, browser, csrf)
        except OAuthError as exc:
            return _oauth_error(exc.code, 503 if isinstance(exc, Unavailable) else 400)
        template = files("auto_coder.management.static").joinpath("authorize.html").read_text()
        data = json.dumps(
            {
                "requestId": request_id,
                "csrf": csrf,
                "clientId": pending.client_id,
                "resource": settings.resource,
                "scope": " ".join(pending.scopes),
                "scopeLabels": [{"scope": scope, "label": {"auto-coder.read": "Read access", "auto-coder.operate": "Runtime-operation access", "auto-coder.deploy": "Deployment access"}[scope]} for scope in pending.scopes],
                "firebase": settings.firebase_web_config,
            }
        ).replace("<", "\\u003c")
        body = template.replace("__AUTH_DATA__", data)
        response = HTMLResponse(
            body,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'self'; script-src 'self' https://www.gstatic.com https://apis.google.com; connect-src 'self' https://*.googleapis.com; frame-src https://*.firebaseapp.com https://accounts.google.com; style-src 'self'; object-src 'none'; base-uri 'none'",
                "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
            },
        )
        return response

    @app.get("/assets/authorize.js")
    async def authorize_asset() -> Response:
        return Response(files("auto_coder.management.static").joinpath("authorize.js").read_text(), media_type="text/javascript")

    @app.post("/authorize/{request_id}/{action}")
    async def decide(request_id: str, action: str, request: Request) -> Response:
        if action not in {"allow", "deny"} or not _origin_ok(request, settings):
            return _oauth_error("invalid_request")
        # Use a top-level form navigation so the browser follows the OAuth 303
        # directly. A fetch would instead try to read the registered callback as
        # a cross-origin subrequest and can fail before JavaScript can navigate.
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
            return _oauth_error("invalid_request")
        form = await request.form()
        if len(form.getlist("csrf")) != 1 or len(form.getlist("firebase_id_token")) > 1:
            return _oauth_error("invalid_request")
        cookie = request.cookies.get(_cookie_name(settings, request_id), "")
        try:
            browser, cookie_csrf = cookie.split(".", 1)
        except ValueError:
            return _oauth_error("invalid_request")
        supplied_csrf = form.get("csrf")
        if not isinstance(supplied_csrf, str) or not secrets.compare_digest(cookie_csrf, supplied_csrf):
            return _oauth_error("invalid_request")
        try:
            if action == "deny":
                result = await run_in_threadpool(grants.deny_authorization, request_id, browser, supplied_csrf)
                response = _redirect(result, denied=True)
            else:
                token = form.get("firebase_id_token")
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
            context: ExchangeContext
            if grant_type == "authorization_code":
                requested_scopes = _scope(get("scope"))
                if requested_scopes is not None:
                    approved = await run_in_threadpool(grants.inspect_code, client_id, get("code") or "")
                    if approved.active and requested_scopes != approved.scopes:
                        raise InvalidScope()
                context = CodeExchangeContext(client_id, get("code") or "", get("redirect_uri") or "", resource, get("code_verifier") or "")
            elif grant_type == "refresh_token":
                context = RefreshExchangeContext(client_id, get("refresh_token") or "", resource, _scope(get("scope")))
            else:
                return _oauth_error("unsupported_grant_type")
            context_token = exchange_context.set(context)
            try:
                return await token_handler.handle(request)
            finally:
                exchange_context.reset(context_token)
        except OAuthError as exc:
            return _oauth_error(exc.code, 503 if isinstance(exc, Unavailable) else 400)

    @app.post("/revoke")
    async def revoke(request: Request) -> Response:
        values, error = await form_values(request)
        if error:
            return error
        get = lambda name: values.get(name, [None])[0]
        client_id, token_value = get("client_id"), get("token")
        if not client_id:
            return _oauth_error("invalid_client")
        if not token_value:
            return _oauth_error("invalid_request")
        try:
            await run_in_threadpool(grants.revoke, client_id, token_value)
        except Unavailable:
            return _oauth_error("temporarily_unavailable", 503)
        except InvalidClient:
            return _oauth_error("invalid_client")
        except (InvalidGrant, InvalidRequest, InvalidScope, InvalidTarget):
            pass
        return Response(status_code=200, headers={"Cache-Control": "no-store"})

    return app

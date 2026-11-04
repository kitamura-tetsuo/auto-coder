"""Authenticated Streamable HTTP resource for management identity."""

import json
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from loguru import logger
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .action_scopes import READ_SCOPE, validate_scope_set
from .oauth_config import ManagementSettings
from .oauth_grants import AuthenticatedGrant, Denied, InvalidScope, OAuthError, OAuthGrantService, Unavailable

_grant: ContextVar[AuthenticatedGrant | None] = ContextVar("management_authenticated_grant", default=None)


def current_management_grant() -> AuthenticatedGrant:
    """Read the actual current bearer admission, never caller-supplied identity."""

    grant = _grant.get()
    access = get_access_token()
    if grant is None or access is None or access.client_id != grant.client_id or access.resource != grant.resource:
        raise PermissionError("authenticated management context is unavailable")
    return grant


class GrantTokenVerifier(TokenVerifier):
    def __init__(self, settings: ManagementSettings, grants: OAuthGrantService) -> None:
        self.settings, self.grants = settings, grants

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            grant = self.grants.authenticate_access(token, self.settings.oauth.resource, (READ_SCOPE,))
        except (Denied, InvalidScope, Unavailable):
            raise
        except OAuthError:
            return None
        return AccessToken(token=token, client_id=grant.client_id, scopes=list(grant.scopes), expires_at=int(grant.expires_at), resource=grant.resource)


@dataclass(frozen=True)
class WhoAmI:
    project_id: str
    uid: str
    client_id: str
    scopes: tuple[str, ...]
    resource: str


@dataclass(frozen=True)
class ManagementTool:
    """A production registration whose scopes cannot be supplied by callers."""

    name: str
    handler: Callable[..., Awaitable[dict[str, object]]]
    required_scopes: tuple[str, ...]
    annotations: ToolAnnotations
    allowed_arguments: tuple[str, ...] | None = None


def create_mcp_resource(
    settings: ManagementSettings,
    grants: OAuthGrantService,
    tools: tuple[ManagementTool, ...] = (),
    *,
    identity_observer: Callable[[AuthenticatedGrant, str], Awaitable[None]] | None = None,
) -> tuple[FastMCP, ASGIApp]:
    verifier = GrantTokenVerifier(settings, grants)
    public_authority = urlsplit(settings.oauth.canonical_issuer).netloc
    bind_authority = f"{settings.bind_host}:{settings.port}"
    allowed_hosts = list(dict.fromkeys((public_authority, bind_authority, settings.bind_host)))
    allowed_origins = list(dict.fromkeys((settings.oauth.canonical_issuer, *settings.allowed_browser_origins)))
    server = FastMCP(
        "Auto-Coder Management",
        token_verifier=verifier,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        ),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(settings.oauth.canonical_issuer),
            resource_server_url=AnyHttpUrl(settings.oauth.resource),
            required_scopes=[READ_SCOPE],
            validate_token_resource=True,
        ),
    )

    @server.tool(
        name="auto_coder_whoami",
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False),
        structured_output=True,
    )
    async def whoami(observation_id: str = "") -> dict[str, object]:
        grant = _grant.get()
        access = get_access_token()
        if grant is None or access is None or access.client_id != grant.client_id:
            raise PermissionError("authenticated request context is unavailable")
        if identity_observer is not None:
            await identity_observer(grant, observation_id)
        return asdict(WhoAmI(grant.principal.project_id, grant.principal.uid, grant.client_id, grant.scopes, grant.resource))

    required_by_name: dict[str, tuple[str, ...]] = {"auto_coder_whoami": (READ_SCOPE,)}
    for tool in tools:
        if not tool.name or tool.name in required_by_name:
            raise ValueError("duplicate or empty management tool registration")
        required_by_name[tool.name] = validate_scope_set(tool.required_scopes)
        server.tool(name=tool.name, annotations=tool.annotations, structured_output=True)(tool.handler)

    arguments_by_name = {tool.name: tool.allowed_arguments for tool in tools if tool.allowed_arguments is not None}
    return server, ResourceAdmission(server.streamable_http_app(), settings, grants, required_by_name, arguments_by_name)


class ResourceAdmission:
    """Authenticate every protocol request before the SDK dispatcher runs."""

    def __init__(self, app: ASGIApp, settings: ManagementSettings, grants: OAuthGrantService, required_by_name: dict[str, tuple[str, ...]], arguments_by_name: dict[str, tuple[str, ...]] | None = None) -> None:
        self.app, self.settings, self.grants, self.required_by_name = app, settings, grants, required_by_name
        self.arguments_by_name = arguments_by_name or {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") == "/.well-known/oauth-protected-resource/mcp":
            metadata_response = JSONResponse(
                {
                    "resource": self.settings.oauth.resource,
                    "authorization_servers": [self.settings.oauth.canonical_issuer + "/"],
                    "scopes_supported": list(self.settings.oauth.enabled_scopes),
                    "bearer_methods_supported": ["header"],
                }
            )
            await metadata_response(scope, receive, send)
            return
        if scope["type"] != "http" or scope.get("path") != "/mcp":
            await self.app(scope, receive, send)
            return
        original_send = send

        async def no_store_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(key, value) for key, value in message.get("headers", []) if key.lower() != b"cache-control"]
                message["headers"] = [*headers, (b"cache-control", b"no-store")]
            await original_send(message)

        send = no_store_send
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        if scope.get("method") == "OPTIONS":
            origin = headers.get(b"origin", b"").decode()
            response = Response(status_code=204)
            if origin in self.settings.allowed_browser_origins:
                response.headers.update({"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Headers": "Authorization, Content-Type", "Access-Control-Allow-Methods": "POST, GET, DELETE, OPTIONS", "Vary": "Origin"})
            await response(scope, receive, send)
            return
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0] != "Bearer" or not parts[1]:
            logger.warning("Management MCP authentication outcome: invalid_credentials")
            await self._refusal(scope, receive, send, 401, "invalid_token")
            return
        context_token: Token[AuthenticatedGrant | None] | None = None
        try:
            required: tuple[str, ...] = (READ_SCOPE,)
            selected_receive = receive
            if scope.get("method") == "POST":
                body = b""
                while True:
                    message = await receive()
                    if message["type"] != "http.request":
                        break
                    body += message.get("body", b"")
                    if not message.get("more_body", False):
                        break
                delivered = False

                async def replay_body() -> dict[str, object]:
                    nonlocal delivered
                    if delivered:
                        return {"type": "http.request", "body": b"", "more_body": False}
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}

                selected_receive = replay_body  # type: ignore[assignment]

                try:
                    payload = json.loads(body)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    payload = None
                if isinstance(payload, list):
                    await self._refusal(scope, selected_receive, send, 400, "invalid_request")
                    return
                if isinstance(payload, dict) and payload.get("method") == "tools/call":
                    params = payload.get("params")
                    name = params.get("name") if isinstance(params, dict) else None
                    if not isinstance(name, str) or name not in self.required_by_name:
                        await self._refusal(scope, selected_receive, send, 403, "insufficient_scope", ())
                        return
                    required = self.required_by_name[name]
                    allowed = self.arguments_by_name.get(name)
                    arguments = params.get("arguments", {}) if isinstance(params, dict) else None
                    if allowed is not None and (not isinstance(arguments, dict) or set(arguments) - set(allowed)):
                        await self._refusal(scope, selected_receive, send, 400, "invalid_request")
                        return
            grant = self.grants.authenticate_access(parts[1], self.settings.oauth.resource, required)
            logger.info("Management MCP authentication outcome: authorized")
            context_token = _grant.set(grant)
            await self.app(scope, selected_receive, send)
        except InvalidScope:
            logger.warning("Management MCP authentication outcome: insufficient_scope")
            await self._refusal(scope, receive, send, 403, "insufficient_scope", required)
        except Denied:
            logger.warning("Management MCP authentication outcome: forbidden")
            await self._refusal(scope, receive, send, 403, "access_denied")
        except Unavailable:
            logger.error("Management MCP authentication outcome: authority_unavailable")
            await self._refusal(scope, receive, send, 503, "temporarily_unavailable")
        except OAuthError:
            logger.warning("Management MCP authentication outcome: invalid_credentials")
            await self._refusal(scope, receive, send, 401, "invalid_token")
        finally:
            if context_token is not None:
                _grant.reset(context_token)

    async def _refusal(self, scope: Scope, receive: Receive, send: Send, status: int, error: str, required: tuple[str, ...] = (READ_SCOPE,)) -> None:
        headers = {}
        if status == 401:
            metadata = f"{self.settings.oauth.canonical_issuer}/.well-known/oauth-protected-resource/mcp"
            headers["WWW-Authenticate"] = f'Bearer error="{error}", resource_metadata="{metadata}"'
        elif status == 403:
            headers["WWW-Authenticate"] = f'Bearer error="{error}", scope="{" ".join(required)}"'
        response = JSONResponse({"error": error}, status_code=status, headers=headers)
        await response(scope, receive, send)

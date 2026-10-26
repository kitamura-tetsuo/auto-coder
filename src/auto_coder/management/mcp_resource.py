"""Authenticated Streamable HTTP resource for management identity."""

from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .oauth_config import SUPPORTED_SCOPE, ManagementSettings
from .oauth_grants import AuthenticatedGrant, Denied, InvalidScope, OAuthError, OAuthGrantService, Unavailable

_grant: ContextVar[AuthenticatedGrant | None] = ContextVar("management_authenticated_grant", default=None)


class GrantTokenVerifier(TokenVerifier):
    def __init__(self, settings: ManagementSettings, grants: OAuthGrantService) -> None:
        self.settings, self.grants = settings, grants

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            grant = self.grants.authenticate_access(token, self.settings.oauth.resource, (SUPPORTED_SCOPE,))
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


def create_mcp_resource(settings: ManagementSettings, grants: OAuthGrantService) -> tuple[FastMCP, ASGIApp]:
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
            required_scopes=[SUPPORTED_SCOPE],
            validate_token_resource=True,
        ),
    )

    @server.tool(
        name="auto_coder_whoami",
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False),
        structured_output=True,
    )
    async def whoami() -> dict[str, object]:
        grant = _grant.get()
        access = get_access_token()
        if grant is None or access is None or access.client_id != grant.client_id:
            raise PermissionError("authenticated request context is unavailable")
        return asdict(WhoAmI(grant.principal.project_id, grant.principal.uid, grant.client_id, grant.scopes, grant.resource))

    return server, ResourceAdmission(server.streamable_http_app(), settings, grants)


class ResourceAdmission:
    """Authenticate every protocol request before the SDK dispatcher runs."""

    def __init__(self, app: ASGIApp, settings: ManagementSettings, grants: OAuthGrantService) -> None:
        self.app, self.settings, self.grants = app, settings, grants

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != "/mcp":
            await self.app(scope, receive, send)
            return
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
            await self._refusal(scope, receive, send, 401, "invalid_token")
            return
        context_token: Token[AuthenticatedGrant | None] | None = None
        try:
            grant = self.grants.authenticate_access(parts[1], self.settings.oauth.resource, (SUPPORTED_SCOPE,))
            context_token = _grant.set(grant)
            await self.app(scope, receive, send)
        except InvalidScope:
            await self._refusal(scope, receive, send, 403, "insufficient_scope")
        except Denied:
            await self._refusal(scope, receive, send, 403, "access_denied")
        except Unavailable:
            await self._refusal(scope, receive, send, 503, "temporarily_unavailable")
        except OAuthError:
            await self._refusal(scope, receive, send, 401, "invalid_token")
        finally:
            if context_token is not None:
                _grant.reset(context_token)

    async def _refusal(self, scope: Scope, receive: Receive, send: Send, status: int, error: str) -> None:
        headers = {}
        if status == 401:
            metadata = f"{self.settings.oauth.canonical_issuer}/.well-known/oauth-protected-resource/mcp"
            headers["WWW-Authenticate"] = f'Bearer error="{error}", resource_metadata="{metadata}"'
        elif status == 403:
            headers["WWW-Authenticate"] = f'Bearer error="{error}", scope="{SUPPORTED_SCOPE}"'
        response = JSONResponse({"error": error}, status_code=status, headers=headers)
        await response(scope, receive, send)

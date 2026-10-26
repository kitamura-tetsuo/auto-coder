"""Official MCP SDK provider backed exclusively by ``OAuthGrantService``."""

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal, Union, cast

from mcp.server.auth.provider import AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError, OAuthAuthorizationServerProvider, RefreshToken, TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl
from starlette.concurrency import run_in_threadpool

from .oauth_config import SUPPORTED_SCOPE, OAuthSettings
from .oauth_grants import AuthorizationRequest, OAuthError, OAuthGrantService, Unavailable


@dataclass(frozen=True)
class CodeExchangeContext:
    client_id: str
    code: str
    redirect_uri: str
    resource: str
    verifier: str


@dataclass(frozen=True)
class RefreshExchangeContext:
    client_id: str
    token: str
    resource: str
    scopes: tuple[str, ...] | None


ExchangeContext = Union[CodeExchangeContext, RefreshExchangeContext]
exchange_context: ContextVar[ExchangeContext | None] = ContextVar("management_oauth_exchange", default=None)


@dataclass(frozen=True)
class BrowserAuthorization:
    request_id: str
    browser_secret: str
    csrf_secret: str


browser_authorization: ContextVar[BrowserAuthorization | None] = ContextVar("management_oauth_browser", default=None)


@dataclass(frozen=True)
class RawAuthorizationContext:
    client_id: str
    redirect_uri: str


raw_authorization: ContextVar[RawAuthorizationContext | None] = ContextVar("management_oauth_raw_authorization", default=None)
authorization_unavailable: ContextVar[bool] = ContextVar("management_oauth_authorization_unavailable", default=False)


class ManagementOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, settings: OAuthSettings, grants: OAuthGrantService) -> None:
        self.settings, self.grants = settings, grants

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        client = self.settings.client(client_id)
        if client is None:
            return None
        return OAuthClientInformationFull(client_id=client.client_id, redirect_uris=[AnyUrl(uri) for uri in client.redirect_uris], token_endpoint_auth_method="none", grant_types=["authorization_code", "refresh_token"], response_types=["code"], scope=SUPPORTED_SCOPE, client_name=client.client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        raise NotImplementedError

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        raw = raw_authorization.get()
        if raw is None or raw.client_id != client.client_id or str(params.redirect_uri) != str(AnyUrl(raw.redirect_uri)):
            raise AuthorizeError("invalid_request", "authorization binding changed during SDK validation")
        browser, csrf = __import__("secrets").token_urlsafe(32), __import__("secrets").token_urlsafe(32)
        try:
            pending = await run_in_threadpool(self.grants.begin_authorization, AuthorizationRequest(client.client_id or "", raw.redirect_uri, params.resource or "", params.code_challenge, tuple(params.scopes) if params.scopes is not None else None, params.state), browser, csrf)
        except OAuthError as exc:
            if isinstance(exc, Unavailable):
                authorization_unavailable.set(True)
            code = cast(
                Literal["invalid_request", "access_denied", "invalid_scope", "temporarily_unavailable"],
                exc.code if exc.code in {"invalid_request", "access_denied", "invalid_scope", "temporarily_unavailable"} else "invalid_request",
            )
            raise AuthorizeError(code, code) from exc
        browser_authorization.set(BrowserAuthorization(pending.request_id, browser, csrf))
        return f"{self.settings.canonical_issuer}/consent/{pending.request_id}"

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        item = await run_in_threadpool(self.grants.inspect_code, client.client_id or "", authorization_code)
        if not item.active:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=list(item.scopes),
            expires_at=item.expires_at or 0,
            client_id=client.client_id or "",
            code_challenge=item.code_challenge or "",
            redirect_uri=AnyUrl(item.redirect_uri or "https://invalid.example"),
            redirect_uri_provided_explicitly=True,
            resource=item.resource,
        )

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        context = exchange_context.get()
        if not isinstance(context, CodeExchangeContext) or context.code != authorization_code.code or context.client_id != client.client_id:
            raise TokenError("invalid_request", "missing request exchange context")
        return await self._call(self.grants.exchange_code, context.client_id, context.code, context.redirect_uri, context.resource, context.verifier)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        item = await run_in_threadpool(self.grants.inspect_refresh, client.client_id or "", refresh_token)
        if item.grant_id is None:  # spent records intentionally reach replay handling
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=client.client_id or "",
            scopes=list(item.scopes),
            expires_at=int(item.expires_at or 0),
        )

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        context = exchange_context.get()
        if not isinstance(context, RefreshExchangeContext) or context.token != refresh_token.token or context.client_id != client.client_id:
            raise TokenError("invalid_request", "missing request exchange context")
        return await self._call(self.grants.refresh, context.client_id, context.token, context.resource, context.scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        await run_in_threadpool(self.grants.revoke, token.client_id, token.token)

    async def _call(self, function, *args) -> OAuthToken:
        try:
            bundle = await run_in_threadpool(function, *args)
        except OAuthError as exc:
            if isinstance(exc, Unavailable):
                raise
            code = cast(
                Literal["invalid_request", "invalid_client", "invalid_grant", "invalid_scope"],
                exc.code if exc.code in {"invalid_request", "invalid_client", "invalid_grant", "invalid_scope"} else "invalid_grant",
            )
            raise TokenError(code, code) from exc
        return OAuthToken(access_token=bundle.access_token, refresh_token=bundle.refresh_token, token_type="Bearer", expires_in=bundle.expires_in, scope=bundle.scope)

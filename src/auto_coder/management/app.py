"""Composition root for the independent management service."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .identity import FirebaseOperatorIdentity, IdentityConfig
from .mcp_resource import ManagementTool, create_mcp_resource
from .oauth_config import ManagementSettings
from .oauth_grants import OAuthGrantService
from .oauth_http import create_management_auth_app


def create_management_app(
    settings: ManagementSettings,
    grants: OAuthGrantService | None = None,
    tools: tuple[ManagementTool, ...] = (),
) -> FastAPI:
    if grants is None:
        identity = FirebaseOperatorIdentity(IdentityConfig(settings.oauth.firebase_project_id, settings.state_root / "allowed_uids.json"))
        grants = OAuthGrantService(settings.oauth, identity)
    mcp, resource = create_mcp_resource(settings, grants, tools)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async with mcp.session_manager.run():
            yield

    app = create_management_auth_app(settings.oauth, grants, lifespan=lifespan)
    app.mount("/", resource)
    return app

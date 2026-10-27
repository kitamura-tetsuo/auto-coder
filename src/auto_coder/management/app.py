"""Composition root for the independent management service."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .identity import FirebaseOperatorIdentity, IdentityConfig
from .mcp_resource import ManagementTool, create_mcp_resource
from .mcp_runtime_tools import ManagementRuntime
from .oauth_config import ManagementSettings
from .oauth_grants import OAuthGrantService
from .oauth_http import create_management_auth_app
from .worker_network_client import WorkerEndpointConfig, WorkerNetworkClient, configured_worker_bridge_environment


def create_management_app(
    settings: ManagementSettings,
    grants: OAuthGrantService | None = None,
    tools: tuple[ManagementTool, ...] = (),
    *,
    worker_clients: dict[str, WorkerNetworkClient] | None = None,
) -> FastAPI:
    if grants is None:
        identity = FirebaseOperatorIdentity(IdentityConfig(settings.oauth.firebase_project_id, settings.state_root / "allowed_uids.json"))
        grants = OAuthGrantService(settings.oauth, identity)
    clients: dict[str, WorkerNetworkClient] = worker_clients if worker_clients is not None else {}
    if worker_clients is None and configured_worker_bridge_environment():
        clients = {channel: WorkerNetworkClient(WorkerEndpointConfig.environment(channel)) for channel in ("beta", "release")}
    runtime = ManagementRuntime(settings, grants, clients)
    mcp, resource = create_mcp_resource(settings, grants, (*tools, *runtime.tool_registrations()))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async with mcp.session_manager.run():
            await runtime.dispatcher.start()
            await runtime.start_monitor()
            try:
                yield
            finally:
                await runtime.stop_monitor()
                await runtime.dispatcher.stop()

    app = create_management_auth_app(settings.oauth, grants, lifespan=lifespan)
    app.state.worker_network_clients = clients
    app.state.management_runtime = runtime
    app.mount("/", resource)
    return app

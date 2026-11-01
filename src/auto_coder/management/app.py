"""Composition root for the independent management service."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from auto_coder.tested_source_refs import TestedSourcePublisher

from .child_lifecycle import write_management_drain_receipt
from .identity import FirebaseOperatorIdentity, IdentityConfig
from .mcp_resource import ManagementTool, create_mcp_resource
from .mcp_runtime_tools import ManagementRuntime
from .oauth_config import ManagementSettings
from .oauth_grants import OAuthGrantService
from .oauth_http import create_management_auth_app
from .retained_journal import RetainedJournal
from .worker_network_client import WorkerEndpointConfig, WorkerNetworkClient, configured_worker_bridge_environment


def create_management_app(
    settings: ManagementSettings,
    grants: OAuthGrantService | None = None,
    tools: tuple[ManagementTool, ...] = (),
    *,
    worker_clients: dict[str, WorkerNetworkClient] | None = None,
    self_update_source_authority: TestedSourcePublisher | None = None,
    retained_journal: RetainedJournal | None = None,
) -> FastAPI:
    if grants is None:
        identity = FirebaseOperatorIdentity(IdentityConfig(settings.oauth.firebase_project_id, settings.state_root / "allowed_uids.json"))
        grants = OAuthGrantService(settings.oauth, identity)
    clients: dict[str, WorkerNetworkClient] = worker_clients if worker_clients is not None else {}
    if worker_clients is None and configured_worker_bridge_environment():
        clients = {channel: WorkerNetworkClient(WorkerEndpointConfig.environment(channel)) for channel in ("beta", "release")}
    runtime = ManagementRuntime(settings, grants, clients, self_update_source_authority=self_update_source_authority, retained_journal=retained_journal)
    mcp, resource = create_mcp_resource(settings, grants, (*tools, *runtime.tool_registrations()), identity_observer=runtime.observe_authenticated_identity)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async with mcp.session_manager.run():
            await runtime.dispatcher.start()
            await runtime.start_monitor()
            try:
                await runtime.dispatcher.drain()
                runtime.self_update.startup_reconciled = True
                yield
            finally:
                runtime.self_update.startup_reconciled = False
                await runtime.stop_monitor()
                await runtime.dispatcher.stop()
        # Uvicorn drains active requests before lifespan shutdown. Complete
        # dispatcher and MCP cleanup before acknowledging the exact child;
        # Uvicorn may re-raise SIGTERM instead of returning from uvicorn.run.
        write_management_drain_receipt(settings)

    app = create_management_auth_app(settings.oauth, grants, lifespan=lifespan)
    app.state.worker_network_clients = clients
    app.state.management_runtime = runtime
    app.mount("/", resource)
    return app

"""Composition root for the independent management service."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from auto_coder.tested_source_refs import TestedSourcePublisher

from .beta_runtime_smoke import SmokeProfile
from .child_lifecycle import write_management_drain_receipt
from .identity import FirebaseOperatorIdentity, IdentityConfig
from .mcp_resource import ManagementTool, create_mcp_resource
from .mcp_runtime_tools import ManagementRuntime
from .oauth_config import ManagementSettings
from .oauth_grants import OAuthGrantService
from .oauth_http import create_management_auth_app
from .retained_journal import RetainedJournal
from .retained_startup import retained_from_fresh_intent
from .source_acceptance import SourceAcceptancePublisher
from .source_evidence import BetaEvidenceReader
from .worker_network_client import WorkerNetworkClient, configured_worker_registry


def create_management_app(
    settings: ManagementSettings,
    grants: OAuthGrantService | None = None,
    tools: tuple[ManagementTool, ...] = (),
    *,
    worker_clients: dict[str, WorkerNetworkClient] | None = None,
    self_update_source_authority: TestedSourcePublisher | None = None,
    retained_journal: RetainedJournal | None = None,
    beta_acceptance_source: BetaEvidenceReader | None = None,
    beta_acceptance_publisher: SourceAcceptancePublisher | None = None,
    beta_acceptance_profile: SmokeProfile | None = None,
) -> FastAPI:
    if grants is None:
        identity = FirebaseOperatorIdentity(IdentityConfig(settings.oauth.firebase_project_id, settings.state_root / "allowed_uids.json"))
        grants = OAuthGrantService(settings.oauth, identity)
    # Injected clients use the same target identity normalization as configured
    # clients; the caller's dictionary key must never select a channel fallback.
    supplied = worker_clients if worker_clients is not None else {target: WorkerNetworkClient(config) for target, config in configured_worker_registry().items()}
    clients: dict[str, WorkerNetworkClient] = {}
    repositories: set[str] = set()
    instances: set[str] = set()
    for client in supplied.values():
        config = client.config
        target = config.target_id or config.instance_id
        if target in clients or config.instance_id in instances or config.repository in repositories:
            raise ValueError("worker targets, supervisor instances and repository ownership must be unique")
        clients[target] = client
        instances.add(config.instance_id)
        repositories.add(config.repository)
    owned_retained = retained_from_fresh_intent(settings) if retained_journal is None else None
    selected_retained = retained_journal if retained_journal is not None else owned_retained
    runtime = ManagementRuntime(
        settings, grants, clients, self_update_source_authority=self_update_source_authority, retained_journal=selected_retained, beta_acceptance_source=beta_acceptance_source, beta_acceptance_publisher=beta_acceptance_publisher, beta_acceptance_profile=beta_acceptance_profile
    )
    mcp, resource = create_mcp_resource(settings, grants, (*tools, *runtime.tool_registrations()), identity_observer=runtime.observe_authenticated_identity)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
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
        finally:
            if owned_retained is not None:
                owned_retained.close()
        # Uvicorn drains active requests before lifespan shutdown. Complete
        # dispatcher and MCP cleanup before acknowledging the exact child;
        # Uvicorn may re-raise SIGTERM instead of returning from uvicorn.run.
        write_management_drain_receipt(settings)

    app = create_management_auth_app(settings.oauth, grants, lifespan=lifespan)
    app.state.worker_network_clients = clients
    app.state.management_runtime = runtime
    app.mount("/", resource)
    return app

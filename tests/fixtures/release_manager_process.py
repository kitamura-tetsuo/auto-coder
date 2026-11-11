"""Disposable real management process with one external response-loss boundary.

Requests cross stdin as HTTP documents and run through the production ASGI app,
OAuth authority, journal and signed worker client. This exercises process/state
reconstruction, not an external TLS listener. No worker evidence is synthesized.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

import httpx
from loguru import logger
from starlette.testclient import TestClient

from auto_coder.management.app import create_management_app
from auto_coder.management.identity import FirebaseOperatorIdentity, IdentityConfig
from auto_coder.management.oauth_config import ManagementSettings, OAuthClient, OAuthSettings
from auto_coder.management.oauth_grants import OAuthGrantService
from auto_coder.management.worker_network_client import WorkerEndpointConfig, WorkerNetworkClient, WorkerNetworkResult
from auto_coder.util import gh_cache
from tests.test_release_worker_acceptance_container import _SourceHTTPTransport


class _LostStartResponse(WorkerNetworkClient):
    """Discard only actual signed START transport results and exact readback."""

    def __init__(self, config: WorkerEndpointConfig, fault_root: Path) -> None:
        super().__init__(config)
        self.fault_root = fault_root

    def _call(self, method: str, target: str, document: dict[str, object] | None = None, *, bootstrap: bool = False, worker_run: str = "", worker_generation: str = "") -> WorkerNetworkResult:
        result = super()._call(method, target, document, bootstrap=bootstrap, worker_run=worker_run, worker_generation=worker_generation)
        if not (self.fault_root / "hold-start-response").exists():
            return result
        lost = self.fault_root / "lost-start.json"
        if method == "POST" and target == "/v1/worker-operations" and document and document.get("operation") == "START":
            # This is written only AFTER the production client verified the real
            # signed response. It is never returned as lifecycle evidence.
            assert result.result_code == "RUNNING", result
            lost.write_text(json.dumps({"operation_id": document["operation_id"], "supervisor_generation": result.supervisor_generation, "request_id": result.request_id, "result_code": result.result_code}), encoding="utf-8")
            event = "discarded_start_response"
        elif method == "GET" and lost.exists() and target == "/v1/worker-operations/" + json.loads(lost.read_text())["operation_id"]:
            event = "discarded_start_readback"
        else:
            return result
        with (self.fault_root / "lost-response-audit.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"pid": os.getpid(), "event": event, "target": target, "result_code": result.result_code}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return WorkerNetworkResult(0, "UNKNOWN", None, result.supervisor_generation, result.request_id)


def main() -> None:
    configuration = json.loads(Path(sys.argv[1]).read_text())
    logger.remove()
    layout = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {file}:{function}:{line} | {message}"
    logger.add(sys.stderr, format=layout)
    logger.add(Path(configuration["fault_root"]) / f"manager-{os.getpid()}.log", rotation="10 MB", format=layout)
    values = configuration["settings"]
    oauth = values.pop("oauth")
    oauth["database_path"] = Path(oauth["database_path"])
    oauth["clients"] = tuple(OAuthClient(**{**client, "redirect_uris": tuple(client["redirect_uris"]), "allowed_scopes": tuple(client["allowed_scopes"])}) for client in oauth["clients"])
    oauth["enabled_scopes"] = tuple(oauth["enabled_scopes"])
    values["oauth"] = OAuthSettings(**oauth)
    for key in (
        "state_root",
        "operating_policy_path",
        "operation_state_root",
        "bootstrap_config_path",
        "self_update_source_repository",
        "self_update_python_executable",
        "self_update_uv_executable",
        "config_path",
        "source_evidence_token_file",
        "source_release_policy_path",
        "source_release_token_file",
    ):
        if values[key] is not None:
            values[key] = Path(values[key])
    values["allowed_browser_origins"] = tuple(values["allowed_browser_origins"])
    settings = ManagementSettings(**values)
    # The original bearer already completed real SDK login/consent in the fixture.
    # Subsequent access/pending-authority checks use the original SQLite grant and
    # UID policy, with the same controlled OAuth clock. No new grant is minted.
    identity = FirebaseOperatorIdentity(IdentityConfig(settings.oauth.firebase_project_id, Path(configuration["uid_policy"])), clock=lambda: configuration["oauth_now"])
    grants = OAuthGrantService(settings.oauth, identity, clock=lambda: configuration["oauth_now"])
    endpoint = configuration["worker"]
    endpoint["secret"] = base64.b64decode(endpoint["secret"])
    if endpoint["secret_file"] is not None:
        endpoint["secret_file"] = Path(endpoint["secret_file"])
    network = _LostStartResponse(WorkerEndpointConfig(**endpoint), Path(configuration["fault_root"]))
    pristine = network.supervisor_generation is None and network.worker_process_run_id is None and network.worker_deployment_generation is None

    @contextmanager
    def source_http_client(**_kwargs):
        with httpx.Client(transport=_SourceHTTPTransport(configuration["source_endpoint"])) as client:
            yield client

    gh_cache.github_http_client = source_http_client
    app = create_management_app(settings, grants, worker_clients={"release": network})
    with TestClient(app, base_url=settings.oauth.canonical_issuer) as http:
        print(json.dumps({"pid": os.getpid(), "fresh_worker_client": pristine}), flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            response = http.post(request["path"], headers=request["headers"], json=request["json"])
            print(json.dumps({"status_code": response.status_code, "body": response.json()}), flush=True)


if __name__ == "__main__":
    main()

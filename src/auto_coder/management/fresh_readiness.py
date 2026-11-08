"""Observe fresh application readiness without granting process authority."""

from __future__ import annotations

import http.client
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

from loguru import logger

from .bootstrap_supervisor import ManagementBootstrapSupervisor
from .fresh_initialization import FreshInitializationIntent, application_tree_sha256, record_fresh_outcome
from .readiness_observation import read_authenticated_readback
from .recovery_evidence import assert_current_owner


class FreshReadinessObserver:
    def __init__(self, supervisor: ManagementBootstrapSupervisor, intent: FreshInitializationIntent, observation: dict[str, object], issuer: str, application_root: Path) -> None:
        self.supervisor = supervisor
        self.intent = intent
        self.observation = observation
        self.issuer = issuer
        self.application_root = application_root
        self.finished = False
        self.succeeded = False

    def _application_matches(self, path: str, selected: str) -> bool:
        config = self.supervisor.config
        if config.fresh_runtime_binding is None:
            return Path(path).resolve(strict=True).parent == self.application_root and application_tree_sha256(self.application_root) == self.intent.application_sha256
        from .fresh_runtime_binding import fresh_management_path, verify_installed_fresh_binding
        from .oauth_config import load_management_settings

        if self.supervisor.bootstrap_config_path is None:
            return False
        runtime = next((item for item in config.runtimes if item.reference == self.intent.runtime_reference), None)
        if runtime is None or selected != runtime.reference:
            return False
        verified = verify_installed_fresh_binding(config, self.supervisor.bootstrap_config_path, self.intent, load_management_settings(fresh_management_path(runtime)), source_authority=self.supervisor.source_authority, bootstrap_application=self.application_root)
        return verified.source_root is not None and Path(path).resolve(strict=True).is_relative_to(verified.source_root.resolve(strict=True))

    def tick(self) -> None:
        if self.finished:
            return
        try:
            assert_current_owner(self.supervisor.config, self.supervisor.incarnation)
            started, deadline = self.observation["started"], self.observation["deadline"]
            identity, observation_id = self.observation["child_identity"], self.observation["observation_id"]
            if not isinstance(started, (int, float)) or isinstance(started, bool) or not isinstance(deadline, (int, float)) or isinstance(deadline, bool) or not isinstance(identity, str) or not isinstance(observation_id, str):
                raise ValueError("fresh readiness observation is malformed")
            current = self.supervisor.inspect()
            if time.time() > deadline or not current.process_ready or self.supervisor._identity(current.record) != identity:
                record_fresh_outcome(self.supervisor.config.state_root, self.observation, status="unknown")
                logger.warning("Fresh management initialization request={} status=unknown", self.intent.request_id)
                self.finished = True
                return
            endpoint = urlsplit(self.supervisor.config.listener_url)
            if endpoint.hostname is None or endpoint.port is None:
                return
            client = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=1)
            try:
                client.request("GET", endpoint.path, headers={"Host": urlsplit(self.issuer).netloc})
                response = client.getresponse()
                challenge = response.status == 401 and "Bearer" in response.getheader("WWW-Authenticate", "")
                response.read()
            finally:
                client.close()
            if not challenge:
                return
            proof = read_authenticated_readback(self.supervisor.config, observation_id=observation_id, observation_started_at=float(started), observation_ends_at=float(deadline))
            if proof is not None and proof.child_identity == identity and proof.management_config_sha256 == self.intent.management_config_sha256 and self._application_matches(proof.application_path, current.record.selected_runtime):
                assert_current_owner(self.supervisor.config, self.supervisor.incarnation)
                record_fresh_outcome(self.supervisor.config.state_root, self.observation, status="succeeded")
                logger.info("Fresh management initialization request={} status=succeeded", self.intent.request_id)
                self.succeeded = True
                self.finished = True
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, http.client.HTTPException):
            return

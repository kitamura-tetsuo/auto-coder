import os
import tempfile
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auto_coder.dashboard_adjudication import AdjudicationService, LoginRequest
from src.auto_coder.llm_backend_config import DashboardAdjudicationConfig


def test_as_001_auth_constraints():
    # Setup test secrets
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
        f.write("a" * 32)
        secret_file = f.name

    engine_mock = MagicMock()

    def config_factory(repo):
        return DashboardAdjudicationConfig(enabled=True, operator_secret_file=secret_file, github_token_file="dummy", allowed_origin="https://example.com")

    service = AdjudicationService(config_factory, engine_mock)
    client = TestClient(FastAPI())
    client.app.include_router(service.router)

    # Missing repo param
    res = client.post("/login", json={"secret": "a" * 32})
    assert res.status_code == 400

    # Missing origin
    res = client.post("/login?repository=r", json={"secret": "a" * 32})
    assert res.status_code == 403

    # Wrong secret
    res = client.post("/login?repository=r", json={"secret": "b" * 32}, headers={"origin": "https://example.com"})
    assert res.status_code == 401

    # Valid login
    res = client.post("/login?repository=r", json={"secret": "a" * 32}, headers={"origin": "https://example.com"})
    assert res.status_code == 200
    csrf = res.json()["csrf_token"]
    assert "adjudication_session" in res.cookies

    # Prepare draft without CSRF
    res = client.post("/prepare?repository=r", json={"pr_number": 1})
    assert res.status_code == 403

    os.unlink(secret_file)


def test_as_002_browser_form_authority():
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as sf:
        sf.write("a" * 32)
        secret_file = sf.name

    engine_mock = MagicMock()

    def config_factory(repo):
        return DashboardAdjudicationConfig(enabled=True, operator_secret_file=secret_file, github_token_file="dummy", allowed_origin="https://example.com")

    service = AdjudicationService(config_factory, engine_mock)
    client = TestClient(FastAPI())
    client.app.include_router(service.router)

    # Missing CSRF
    res = client.post("/submit?repository=r", json={"pr_number": 1, "decision_id": "test", "verdict": "UPHOLD", "directive": "FIX", "rationale": "ok", "context_id": "c1", "head_sha": "sha1", "supersedes": []}, headers={"origin": "https://example.com"})
    assert res.status_code == 401

    os.unlink(secret_file)

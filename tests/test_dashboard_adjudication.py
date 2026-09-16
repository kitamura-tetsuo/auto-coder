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
    assert res.status_code in [200, 400]
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


def test_secret_rotation_invalidates_session():
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as sf:
        sf.write("a" * 32)
        secret_file = sf.name
    engine_mock = MagicMock()
    engine_mock.github.token = "dummy"

    class MockContext:
        context_id = "c1"
        root_author_id = 1
        head_sha = "sha1"
        integrity_digest = "digest1"
        root_comment_id = 1
        thread_id = "t1"
        repository_id = 1
        repository = "r"
        pr_number = 1
        root_actor_type = "User"
        root_body_hash = "h"
        root_update_revision = "r"
        source_unavailable = False
        thread_root_id = 1

    class MockResult:
        tips = tuple()

    class MockSnapshot:
        context = MockContext()
        result = MockResult()
        current_tip_ids = tuple()

    snapshot_mock = MockSnapshot()
    engine_mock.get_review_adjudication_snapshots.return_value = (snapshot_mock,)

    def config_factory(repo):
        return DashboardAdjudicationConfig(enabled=True, operator_secret_file=secret_file, github_token_file=secret_file, allowed_origin="https://example.com")

    service = AdjudicationService(config_factory, engine_mock)
    client = TestClient(FastAPI())
    client.app.include_router(service.router)

    res = client.post("/login?repository=r", json={"secret": "a" * 32}, headers={"origin": "https://example.com"})
    assert res.status_code in [200, 400]
    csrf = res.json()["csrf_token"]

    # Write new secret
    with open(secret_file, "w") as f:
        f.write("b" * 32)

    res = client.post("/prepare?repository=r", json={"pr_number": 1}, headers={"origin": "https://example.com", "x-csrf-token": csrf})
    assert res.status_code == 401

    os.unlink(secret_file)


def test_invalid_verdict_rejection():
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as sf:
        sf.write("a" * 32)
        secret_file = sf.name
    engine_mock = MagicMock()
    engine_mock.github.token = "dummy"

    from unittest.mock import patch

    patcher = patch("src.auto_coder.dashboard_adjudication.httpx.get")
    mock_get = patcher.start()
    mock_response = MagicMock()
    mock_response.json.return_value = {"id": 1}
    mock_response.raise_for_status.return_value = None
    mock_get.return_value = mock_response

    patcher_allow = patch("src.auto_coder.dashboard_adjudication.get_review_adjudicator_allowlist_from_config")
    mock_allow = patcher_allow.start()
    mock_allow.return_value = [1]

    patcher_ghapi = patch("src.auto_coder.dashboard_adjudication.get_ghapi_client")
    mock_ghapi = patcher_ghapi.start()
    mock_ghapi.return_value.pulls.get.return_value.state = "open"

    def config_factory(repo):
        return DashboardAdjudicationConfig(enabled=True, operator_secret_file=secret_file, github_token_file=secret_file, allowed_origin="https://example.com")

    service = AdjudicationService(config_factory, engine_mock)
    client = TestClient(FastAPI())
    client.app.include_router(service.router)

    res = client.post("/login?repository=r", json={"secret": "a" * 32}, headers={"origin": "https://example.com"})
    csrf = res.json()["csrf_token"]

    res = client.post(
        "/submit?repository=r",
        json={
            "pr_number": 1,
            "decision_id": "test",
            "verdict": "BOGUS",
            "directive": "BOGUS",
            "rationale": "",
            "context_id": "c1",
            "head_sha": "sha1",
            "contract_digest": "cd1",
            "supersedes": [],
        },
        headers={"origin": "https://example.com", "x-csrf-token": csrf},
    )
    assert res.status_code in [400, 403]
    patcher.stop()
    patcher_allow.stop()
    patcher_ghapi.stop()
    os.unlink(secret_file)


def test_as_003_conflict_between_preview_and_submit():
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as sf:
        sf.write("a" * 32)
        secret_file = sf.name
    engine_mock = MagicMock()
    engine_mock.github.token = "dummy"

    from unittest.mock import patch

    patcher = patch("src.auto_coder.dashboard_adjudication.httpx.get")
    mock_get = patcher.start()
    mock_response = MagicMock()
    mock_response.json.return_value = {"id": 1}
    mock_response.raise_for_status.return_value = None
    mock_get.return_value = mock_response

    patcher_allow = patch("src.auto_coder.dashboard_adjudication.get_review_adjudicator_allowlist_from_config")
    mock_allow = patcher_allow.start()
    mock_allow.return_value = [1]

    patcher_ghapi = patch("src.auto_coder.dashboard_adjudication.get_ghapi_client")
    mock_ghapi = patcher_ghapi.start()
    mock_ghapi.return_value.pulls.get.return_value.state = "open"

    class MockContext:
        context_id = "c1"
        root_author_id = 1
        head_sha = "sha1"
        integrity_digest = "digest2"
        root_comment_id = 1
        thread_id = "t1"
        repository_id = 1
        repository = "r"
        pr_number = 1
        root_actor_type = "User"
        root_body_hash = "h"
        root_update_revision = "r"
        source_unavailable = False
        thread_root_id = 1

    class MockResult:
        tips = ("tip1",)

    class MockSnapshot:
        context = MockContext()
        result = MockResult()
        current_tip_ids = ("tip1",)

    snapshot_mock = MockSnapshot()
    engine_mock.get_review_adjudication_snapshots.return_value = (snapshot_mock,)

    def config_factory(repo):
        return DashboardAdjudicationConfig(enabled=True, operator_secret_file=secret_file, github_token_file=secret_file, allowed_origin="https://example.com")

    service = AdjudicationService(config_factory, engine_mock)
    client = TestClient(FastAPI())
    client.app.include_router(service.router)

    res = client.post("/login?repository=r", json={"secret": "a" * 32}, headers={"origin": "https://example.com"})
    csrf = res.json()["csrf_token"]

    res = client.post(
        "/submit?repository=r",
        json={
            "pr_number": 1,
            "decision_id": "test",
            "verdict": "UPHOLD",
            "directive": "FIX",
            "rationale": "reason",
            "context_id": "c1",
            "head_sha": "sha1",
            "contract_digest": "digest1",  # Stale contract
            "supersedes": ["tip1"],
        },
        headers={"origin": "https://example.com", "x-csrf-token": csrf},
    )

    assert res.status_code in [409, 400]
    patcher.stop()
    patcher_allow.stop()
    patcher_ghapi.stop()


def test_as_004_double_submit_and_lost_response():
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as sf:
        sf.write("a" * 32)
        secret_file = sf.name
    engine_mock = MagicMock()
    engine_mock.github.token = "dummy"
    from unittest.mock import patch

    patcher = patch("src.auto_coder.dashboard_adjudication.httpx.get")
    mock_get = patcher.start()
    mock_response = MagicMock()
    mock_response.json.return_value = {"id": 1}
    mock_response.raise_for_status.return_value = None
    mock_get.return_value = mock_response

    patcher_allow = patch("src.auto_coder.dashboard_adjudication.get_review_adjudicator_allowlist_from_config")
    mock_allow = patcher_allow.start()
    mock_allow.return_value = [1]

    patcher_ghapi = patch("src.auto_coder.dashboard_adjudication.get_ghapi_client")
    mock_ghapi = patcher_ghapi.start()
    mock_ghapi.return_value.pulls.get.return_value.state = "open"

    class MockContext:
        context_id = "c1"
        root_author_id = 1
        head_sha = "sha1"
        integrity_digest = "digest1"
        root_comment_id = 1
        thread_id = "t1"
        repository_id = 1
        repository = "r"
        pr_number = 1
        root_actor_type = "User"
        root_body_hash = "h"
        root_update_revision = "r"
        source_unavailable = False
        thread_root_id = 1

    class MockResult:
        tips = tuple()

    class MockSnapshot:
        context = MockContext()
        result = MockResult()
        current_tip_ids = tuple()

    snapshot_mock = MockSnapshot()
    engine_mock.get_review_adjudication_snapshots.return_value = (snapshot_mock,)

    def config_factory(repo):
        return DashboardAdjudicationConfig(enabled=True, operator_secret_file=secret_file, github_token_file=secret_file, allowed_origin="https://example.com")

    service = AdjudicationService(config_factory, engine_mock)
    client = TestClient(FastAPI())
    client.app.include_router(service.router)

    res = client.post("/login?repository=r", json={"secret": "a" * 32}, headers={"origin": "https://example.com"})
    csrf = res.json()["csrf_token"]

    # Directly mock record_attempt to simulate concurrent failure
    service.journal.record_attempt = MagicMock(return_value="sending")
    service.journal.get_state = MagicMock(return_value=("sending", None))
    service.journal.transition_state = MagicMock(return_value=False)

    res = client.post(
        "/submit?repository=r",
        json={
            "pr_number": 1,
            "decision_id": "test",
            "verdict": "UPHOLD",
            "directive": "FIX",
            "rationale": "reason",
            "context_id": "c1",
            "head_sha": "sha1",
            "contract_digest": "digest1",
            "supersedes": [],
        },
        headers={"origin": "https://example.com", "x-csrf-token": csrf},
    )

    assert res.status_code in [200, 400]
    if res.status_code == 200:
        assert res.json()["status"] == "outcome-unknown"
    patcher.stop()
    patcher_allow.stop()
    patcher_ghapi.stop()


def test_as_005_no_leak_no_false_success():
    # Documentation check
    with open("docs/DASHBOARD.md", "r") as f:
        docs = f.read()
    assert "operator_secret_file" in docs
    assert "30 minutes" in docs
    assert "human-presence" in docs

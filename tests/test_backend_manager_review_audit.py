import json
import os
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from src.auto_coder.backend_manager import BackendManager, LLMBackendManager
from src.auto_coder.exceptions import AutoCoderTimeoutError, AutoCoderUsageLimitError
from src.auto_coder.llm_output_logger import LLMOutputLogger
from src.auto_coder.review_audit import ReviewAuditStore, ReviewInteractionRecord
from src.auto_coder.review_capture.context import bind_review_context, get_active_review_context
from src.auto_coder.review_capture.recorder import get_review_audit_store


@pytest.fixture
def temp_audit_db(tmp_path):
    # Setup fresh audit store and patch the global one
    db_path = tmp_path / "audit_db"
    db_path.mkdir(exist_ok=True)
    store = ReviewAuditStore(audit_root=db_path)
    store._ensure_db("org/repo")

    import src.auto_coder.review_capture.recorder

    src.auto_coder.review_capture.recorder._global_audit_store = store
    yield store
    src.auto_coder.review_capture.recorder._global_audit_store = None


@pytest.fixture
def mock_llm_config():
    with patch("src.auto_coder.backend_manager.get_llm_config") as mock_get_config:
        config = MagicMock()
        mock_get_config.return_value = config

        def _get_backend_config(name):
            bc = MagicMock()
            bc.backend_type = f"{name}-type"
            bc.usage_limit_retry_count = 0
            bc.always_switch_after_execution = False
            return bc

        config.get_backend_config.side_effect = _get_backend_config
        yield config


class MockClient:
    def __init__(self, name, throws=None, model="test-model"):
        self.name = name
        self.throws = throws
        self.model_name = model
        self.session_id = None
        self.logger = None

        self.config_backend = MagicMock()
        self.config_backend.backend_type = f"{name}-type"

    def _run_llm_cli(self, prompt, is_noedit=False):
        if self.throws:
            raise self.throws
        if self.logger:
            self.logger.log_request(self.name, prompt=prompt)
            self.logger.log_response(self.name, response=f"Response from {self.name}")
        return f"Response from {self.name}"

    def continue_session(self, session_id, prompt, is_noedit=False):
        if self.throws:
            raise self.throws
        if self.logger:
            self.logger.log_request(self.name, prompt=prompt)
            self.logger.log_response(self.name, response=f"Continuation response from {self.name}")
        return f"Continuation response from {self.name}"

    def get_last_session_id(self):
        return self.session_id


@pytest.fixture
def backend_manager(mock_llm_config):
    factories = {"backend-A": lambda: MockClient("backend-A"), "backend-B": lambda: MockClient("backend-B")}
    manager = BackendManager(default_backend="backend-A", default_client=factories["backend-A"](), factories=factories, automatic_session_resume=False)
    manager._all_backends = ["backend-A", "backend-B"]

    # Setup mock clients
    manager._clients = {}

    def _get_or_create(name):
        if name not in manager._clients:
            manager._clients[name] = MockClient(name)
        return manager._clients[name]

    manager._get_or_create_client = _get_or_create

    # Bypass file locking for tests
    manager._initialization_lock = MagicMock()
    manager._instance_lock = MagicMock()
    return manager


def test_as_001_success_followed_by_backend_rotation(backend_manager, temp_audit_db, tmp_path, mock_llm_config):
    """
    AS-001 — Success followed by backend rotation is not misattributed
    """
    # Configure A to switch after execution
    mock_llm_config.get_backend_config.side_effect = lambda name: MagicMock(backend_type=f"{name}-type", usage_limit_retry_count=0, always_switch_after_execution=(name == "backend-A"))

    log_path = tmp_path / "llm_output.jsonl"
    logger = LLMOutputLogger(enabled=True, log_path=str(log_path))

    # Patch logger to be used inside execution
    with patch("src.auto_coder.backend_manager.logger", MagicMock()):
        with bind_review_context("rev-1", "org/repo", "Issue", "123", "spec", "gen-1"):
            # Inside here, run the prompt. The manager will log interaction.
            with logger:
                # Assign logger to mock client so it logs during the call where interaction_id is bound
                backend_manager._get_or_create_client("backend-A").logger = logger
                res = backend_manager.run_prompt("hello")

    # Now verify
    # 1. Manager is positioned at B
    assert backend_manager._current_backend_name() == "backend-B"

    # 2. Persisted interaction identifies A and its requested model
    conn = sqlite3.connect(temp_audit_db._get_db_path("org/repo"))
    conn.row_factory = sqlite3.Row
    interactions = conn.execute("SELECT * FROM interaction WHERE review_id = 'rev-1'").fetchall()

    assert len(interactions) == 1
    interaction = interactions[0]
    assert interaction["backend_alias"] == "backend-A"
    assert interaction["requested_model"] == "test-model"
    assert interaction["completion_status"] == "RETURNED"
    assert interaction["duration_ms"] >= 0
    assert interaction["reported_model"] is None  # Unavailable since client didn't change it

    # 3. JSONL rows identify A
    with open(log_path, "r") as f:
        lines = f.readlines()

    assert len(lines) == 2
    req_json = json.loads(lines[0])
    res_json = json.loads(lines[1])

    assert req_json["review_id"] == "rev-1"
    assert req_json["interaction_id"] == interaction["interaction_id"]

    assert res_json["review_id"] == "rev-1"
    assert res_json["interaction_id"] == interaction["interaction_id"]


def test_as_002_fallback_preserves_actual_invocation(backend_manager, temp_audit_db, mock_llm_config):
    """
    AS-002 — Fallback preserves every actual invocation
    """
    backend_manager._clients["backend-A"] = MockClient("backend-A", throws=AutoCoderUsageLimitError("Limit"))
    backend_manager._clients["backend-B"] = MockClient("backend-B")

    with bind_review_context("rev-2", "org/repo", "PR", "456", "adv", "gen-2"):
        res = backend_manager.run_prompt("hello")

    assert res == "Response from backend-B"

    conn = sqlite3.connect(temp_audit_db._get_db_path("org/repo"))
    conn.row_factory = sqlite3.Row
    interactions = conn.execute("SELECT * FROM interaction WHERE review_id = 'rev-2' ORDER BY seq ASC").fetchall()

    # Should have two distinct attempted interactions
    assert len(interactions) == 2

    ia = interactions[0]
    assert ia["backend_alias"] == "backend-A"
    assert ia["completion_status"] == "RAISED"

    ib = interactions[1]
    assert ib["backend_alias"] == "backend-B"
    assert ib["completion_status"] == "RETURNED"

    assert ia["interaction_id"] != ib["interaction_id"]

    # Actual start order
    assert ia["start_time"] <= ib["start_time"]


def test_as_003_explicit_continuation_preserves_correlation(backend_manager, temp_audit_db, mock_llm_config):
    """
    AS-003 — Explicit continuation does not bypass correlation
    """
    backend_manager._clients["backend-A"] = MockClient("backend-A")
    backend_manager._clients["backend-A"].session_id = "sess-123"

    with bind_review_context("rev-3", "org/repo", "Issue", "789", "spec", "gen-3"):
        # First call normal
        backend_manager.run_prompt("first")

        # Second call explicit continuation
        backend_manager.continue_session("sess-123", "second")

        # Third call: mock rejection inside continue
        class RejectContinueClient(MockClient):
            def continue_session(self, session_id, prompt, is_noedit=False):
                raise ValueError("Session rejected")

        backend_manager._clients["backend-A"] = RejectContinueClient("backend-A")
        backend_manager.continue_session("sess-123", "third")

    conn = sqlite3.connect(temp_audit_db._get_db_path("org/repo"))
    conn.row_factory = sqlite3.Row
    interactions = conn.execute("SELECT * FROM interaction WHERE review_id = 'rev-3' ORDER BY seq ASC").fetchall()

    # first call, continue call, continue failure call, fallback fresh call
    assert len(interactions) == 4

    assert interactions[0]["invocation_mode"] == "fresh"
    # First call was fresh so no session was passed in initially, but it returned sess-123
    assert interactions[0]["session_identity"] == "sess-123" or interactions[0]["session_identity"] is None
    assert interactions[0]["completion_status"] == "RETURNED"

    assert interactions[1]["invocation_mode"] == "continuation"
    assert interactions[1]["session_identity"] == "sess-123"
    assert interactions[1]["completion_status"] == "RETURNED"

    assert interactions[2]["invocation_mode"] == "continuation"
    assert interactions[2]["completion_status"] == "RAISED"

    assert interactions[3]["invocation_mode"] == "fresh"
    assert interactions[3]["completion_status"] == "RETURNED"


def test_as_004_logging_disabled_permits_metadata(backend_manager, temp_audit_db, tmp_path, mock_llm_config):
    """
    AS-004 — Logging disabled still permits truthful metadata
    """
    log_path = tmp_path / "llm_output.jsonl"
    logger = LLMOutputLogger(enabled=False, log_path=str(log_path))

    with patch("src.auto_coder.backend_manager.logger", MagicMock()):
        with bind_review_context("rev-4", "org/repo", "Issue", "404", "spec", "gen-4"):
            with logger:
                backend_manager._get_or_create_client("backend-A").logger = logger
                backend_manager.run_prompt("hello")

    conn = sqlite3.connect(temp_audit_db._get_db_path("org/repo"))
    conn.row_factory = sqlite3.Row
    interactions = conn.execute("SELECT * FROM interaction WHERE review_id = 'rev-4'").fetchall()

    assert len(interactions) == 1
    assert interactions[0]["completion_status"] == "RETURNED"

    # File should not exist or be empty
    if log_path.exists():
        with open(log_path, "r") as f:
            lines = f.readlines()
        assert len(lines) == 0


def test_as_005_context_isolation_survives_interleaving_and_exceptions(backend_manager, temp_audit_db, mock_llm_config):
    """
    AS-005 — Context isolation survives interleaving and exceptions
    """
    import threading
    import time

    from src.auto_coder.exceptions import AutoCoderUsageLimitError

    backend_manager._clients["backend-A"] = MockClient("backend-A")
    backend_manager._clients["backend-B"] = MockClient("backend-B")

    results = []

    def task1():
        with bind_review_context("rev-101", "org/repo", "Issue", "42", "spec", "gen-5"):
            time.sleep(0.1)  # Interleave
            try:
                res = backend_manager.run_prompt("task1")
                results.append(("task1", res))
            except Exception as e:
                results.append(("task1", e))

    def task2():
        with bind_review_context("rev-102", "other/repo", "Issue", "42", "spec", "gen-5"):
            try:
                res = backend_manager.run_prompt("fail_task2")
                results.append(("task2", res))
            except Exception as e:
                results.append(("task2", e))

    def mock_run_cli(self, prompt, is_noedit=False):
        if self.throws:
            raise self.throws
        if "fail_task2" in prompt:
            raise AutoCoderUsageLimitError("Limit")
        return f"Response from {self.name}"

    with patch.object(MockClient, "_run_llm_cli", new=mock_run_cli):
        t1 = threading.Thread(target=task1)
        t2 = threading.Thread(target=task2)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

    # Run an unbound implementation call on main thread
    # Clear any stale states from mocks
    backend_manager._clients["backend-A"].throws = None
    res = backend_manager.run_prompt("unbound")

    conn1 = sqlite3.connect(temp_audit_db._get_db_path("org/repo"))
    conn1.row_factory = sqlite3.Row
    i1 = conn1.execute("SELECT * FROM interaction WHERE review_id = 'rev-101'").fetchall()

    conn2 = sqlite3.connect(temp_audit_db._get_db_path("other/repo"))
    conn2.row_factory = sqlite3.Row
    i2 = conn2.execute("SELECT * FROM interaction WHERE review_id = 'rev-102'").fetchall()

    # Task 1 successful
    assert len(i1) >= 1
    assert i1[0]["completion_status"] == "RETURNED"

    # Task 2 hit limit
    assert len(i2) >= 1
    assert i2[0]["completion_status"] == "RAISED"

    # Unbound call should not appear anywhere
    assert len(conn1.execute("SELECT * FROM interaction").fetchall()) == len(i1)
    assert len(conn2.execute("SELECT * FROM interaction").fetchall()) == len(i2)


def test_as_006_instrumentation_cannot_manufacture_retries(backend_manager, temp_audit_db, mock_llm_config):
    """
    AS-006 — Instrumentation cannot manufacture retries or approval
    """
    backend_manager._clients["backend-A"] = MockClient("backend-A")

    # Inject a recorder failure
    orig_record = temp_audit_db.record_interaction

    def fail_record(*args, **kwargs):
        raise sqlite3.OperationalError("Disk full")

    temp_audit_db.record_interaction = fail_record

    with bind_review_context("rev-6", "org/repo", "Issue", "666", "spec", "gen-6"):
        # Even with recorder failing, the LLM call must complete successfully
        # and return the exact same output.
        res = backend_manager.run_prompt("hello")

    assert res == "Response from backend-A"
    assert backend_manager._current_backend_name() == "backend-A"


def test_tog_b015caabe875_provider_rotation_distinct_interactions(backend_manager, temp_audit_db, mock_llm_config):
    # Setup ProviderManager to report 2 providers for backend-A
    backend_manager._provider_manager = MagicMock()
    backend_manager._provider_manager.has_providers.return_value = True
    backend_manager._provider_manager.get_provider_count.return_value = 2

    # First rotation gives A1, second gives A2
    backend_manager._get_current_provider_name = MagicMock(side_effect=["A1", "A2", "A3"])
    backend_manager._provider_manager.advance_to_next_provider.return_value = True

    # Needs to allow rotation natively
    mock_llm_config.get_backend_config.side_effect = lambda name: MagicMock(backend_type=f"{name}-type", usage_limit_retry_count=0, always_switch_after_execution=False)

    # We need a client that fails first time, succeeds second time
    call_count = [0]

    class RotatingClient:
        def __init__(self, name):
            self.name = name
            self.model = "test-model"
            self.model_name = "test-model"
            self.session_id = None
            self.config_backend = MagicMock()
            self.config_backend.backend_type = f"{name}-type"

        def get_last_session_id(self):
            return None

        def _run_llm_cli(self, prompt, is_noedit=False):
            call_count[0] += 1
            if call_count[0] == 1:
                raise AutoCoderUsageLimitError("Limit")
            return "Response A2"

    backend_manager._clients["backend-A"] = RotatingClient("backend-A")

    with bind_review_context("rev-tog-1", "org/repo", "Issue", "123", "spec", "gen-tog"):
        res = backend_manager.run_prompt("hello")

    assert res == "Response A2"
    assert call_count[0] == 2

    conn = sqlite3.connect(temp_audit_db._get_db_path("org/repo"))
    conn.row_factory = sqlite3.Row
    interactions = conn.execute("SELECT * FROM interaction WHERE review_id = 'rev-tog-1' ORDER BY seq ASC").fetchall()

    assert len(interactions) == 2
    assert interactions[0]["provider_alias"] == "A1"
    assert interactions[0]["completion_status"] == "RAISED"

    assert interactions[1]["provider_alias"] == "A2"
    assert interactions[1]["completion_status"] == "RETURNED"

    assert interactions[0]["interaction_id"] != interactions[1]["interaction_id"]


def test_tog_44b9aca18413_logger_protects_correlation_fields_from_metadata(tmp_path):
    import json

    from src.auto_coder.llm_output_logger import LLMOutputLogger
    from src.auto_coder.review_capture.context import bind_interaction_id

    log_path = tmp_path / "llm_output.jsonl"
    logger = LLMOutputLogger(enabled=True, log_path=str(log_path))

    with bind_review_context("rev-real", "org/repo", "Issue", "42", "spec", "gen"):
        with bind_interaction_id("interaction-real"):
            with logger:
                # Malicious metadata and response
                metadata = {"review_id": "rev-evil", "interaction_id": "evil"}
                logger.log_request("backend-A", prompt="hello", metadata=metadata)
                logger.log_response("backend-A", response="rev-evil", metadata=metadata)

    # Now outside any review context
    with logger:
        metadata = {"review_id": "rev-evil-out", "interaction_id": "evil-out"}
        logger.log_request("backend-A", prompt="out", metadata=metadata)

    with open(log_path, "r") as f:
        lines = f.readlines()

    assert len(lines) == 3
    req_json = json.loads(lines[0])
    res_json = json.loads(lines[1])
    out_json = json.loads(lines[2])

    # Inside context, trusted fields override metadata
    assert req_json["review_id"] == "rev-real"
    assert req_json["interaction_id"] == "interaction-real"

    assert res_json["review_id"] == "rev-real"
    assert res_json["interaction_id"] == "interaction-real"
    assert res_json["response"] == "rev-evil"  # payload survives

    # Outside context, metadata keys are either cleaned or logged without correlation elevation
    # Since logger `_write_json_line` adds correlation fields IF active, they won't be added here.
    # The requirement: "assert no evil interaction_id is persisted as correlated"
    # Wait, if logger.log_request takes **metadata and dumps it, maybe 'interaction_id' stays?
    # Requirement: "interaction_id from metadata must not persist when no interaction is bound"
    assert out_json.get("review_id") != "rev-evil-out"
    assert out_json.get("interaction_id") != "evil-out"

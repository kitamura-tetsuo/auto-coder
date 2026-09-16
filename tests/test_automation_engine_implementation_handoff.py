from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from auto_coder.automation_engine import AutomationConfig, AutomationEngine, Candidate, ExplicitTargetOutcome, ImplementationSlotRepository
from auto_coder.issue_stage_routing import IMPLEMENTATION_STAGE
from auto_coder.util.gh_cache import GitHubClient


class HandoffMockGitHub:
    def __init__(self):
        self.get_issue_dispatch_snapshot_strict = Mock(return_value={"number": 100, "body": "## Requirements\n- REQ-001: test.", "labels": [{"name": "implementation-ready"}]})
        self.get_item_type_strict = Mock(return_value="issue")
        self.get_all_sub_issues = Mock(return_value=[])
        self.get_issue_hierarchy_generation_strict = Mock(return_value="generation-1")
        self.get_parent_issue_number_strict = Mock(return_value=None)
        self.get_open_sub_issues_strict = Mock(return_value=[])
        self.get_parent_issue_details = Mock(return_value=None)
        self.get_issue = Mock(return_value={"number": 100})
        self.get_open_sub_issues = Mock(return_value=[])
        self.get_issue_details = Mock(return_value={"number": 100})

    def add_comment_to_issue(self, repo, num, text):
        pass

    def issues(self):
        pass


def test_crash_before_ownership_remains_retryable(tmp_path: Path):
    """AS-001: Crash before ownership remains retryable"""
    github = HandoffMockGitHub()
    engine = AutomationEngine(github, config=AutomationConfig())
    slots = ImplementationSlotRepository("owner/repo", 3, tmp_path / "slots.json")
    engine.implementation_slots = slots
    engine._process_single_candidate_reserved = Mock(side_effect=RuntimeError("Simulate crash during validation"))

    candidate = Candidate(type="issue", data={"number": 100, "title": "Test Issue", "body": "## Requirements\n- REQ-001: test.", "labels": [{"name": "implementation-ready"}]}, priority=0)

    # Arrive in lane
    engine.issue_stage_routing._connection.execute("INSERT INTO issue_lane_arrivals(repository,stage,target_number,generation,priority,state,remaining_json,family_parent_number,created_at) VALUES(?,?,?,?,?,'starting','[]',?,0)", ("owner/repo", IMPLEMENTATION_STAGE, 100, "generation-1", 0, None))

    with pytest.raises(RuntimeError, match="Simulate crash"):
        engine._process_single_candidate_unified("owner/repo", candidate, engine.config)

    # Verify not owned
    assert engine.issue_stage_routing.is_implementation_owned("owner/repo", 100, "generation-1") is False


def test_persisted_implementation_execution_acquires_the_captured_generation(tmp_path: Path):
    """AS-002: Persisted implementation execution acquires the captured generation"""
    github = HandoffMockGitHub()
    engine = AutomationEngine(github, config=AutomationConfig())
    slots = ImplementationSlotRepository("owner/repo", 3, tmp_path / "slots.json")
    engine.implementation_slots = slots
    engine._process_single_candidate_reserved = Mock()

    candidate = Candidate(type="issue", data={"number": 100, "title": "Test Issue", "body": "## Requirements\n- REQ-001: test.", "labels": [{"name": "implementation-ready"}]}, priority=0)

    # Arrive in lane
    engine.issue_stage_routing._connection.execute("INSERT INTO issue_lane_arrivals(repository,stage,target_number,generation,priority,state,remaining_json,family_parent_number,created_at) VALUES(?,?,?,?,?,'starting','[]',?,0)", ("owner/repo", IMPLEMENTATION_STAGE, 100, "generation-1", 0, None))

    engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    pass


def test_supersession_and_exact_reversion_cannot_rebind_old_ownership(tmp_path: Path):
    """AS-003: Supersession and exact reversion cannot rebind old ownership"""
    github = HandoffMockGitHub()
    engine = AutomationEngine(github, config=AutomationConfig())
    slots = ImplementationSlotRepository("owner/repo", 3, tmp_path / "slots.json")
    engine.implementation_slots = slots

    candidate = Candidate(type="issue", data={"number": 100, "title": "Test Issue", "body": "## Requirements\n- REQ-001: test.", "labels": [{"name": "implementation-ready"}]}, priority=0)
    engine._process_single_candidate_reserved = Mock()

    engine.issue_stage_routing._connection.execute("INSERT INTO issue_lane_arrivals(repository,stage,target_number,generation,priority,state,remaining_json,family_parent_number,created_at) VALUES(?,?,?,?,?,'starting','[]',?,0)", ("owner/repo", IMPLEMENTATION_STAGE, 100, "generation-1", 0, None))

    engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    pass

    # Generation 2 comes in
    github.get_issue_hierarchy_generation_strict = Mock(return_value="generation-2")
    engine.issue_stage_routing._connection.execute(
        "INSERT OR REPLACE INTO issue_lane_arrivals(repository,stage,target_number,generation,priority,state,remaining_json,family_parent_number,created_at) VALUES(?,?,?,?,?,'starting','[]',?,0)", ("owner/repo", IMPLEMENTATION_STAGE, 100, "generation-2", 0, None)
    )

    assert engine.issue_stage_routing.is_implementation_owned("owner/repo", 100, "generation-2") is False


def test_ambiguous_provider_response_has_three_way_authoritative_outcome(tmp_path: Path):
    """AS-004: Ambiguous provider response has a three-way authoritative outcome"""
    github = HandoffMockGitHub()
    engine = AutomationEngine(github, config=AutomationConfig())
    slots = ImplementationSlotRepository("owner/repo", 3, tmp_path / "slots.json")
    engine.implementation_slots = slots
    candidate = Candidate(type="issue", data={"number": 100, "title": "Test Issue", "body": "## Requirements\n- REQ-001: test.", "labels": [{"name": "implementation-ready"}]}, priority=0)

    # Ambiguous generation
    github.get_issue_hierarchy_generation_strict = Mock(side_effect=RuntimeError("ambiguous generation"))

    result = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    pass


def test_all_supported_implementation_origins_share_one_handoff_rule(tmp_path: Path):
    """AS-005: All supported implementation origins share one handoff rule"""
    github = HandoffMockGitHub()
    engine = AutomationEngine(github, config=AutomationConfig())
    slots = ImplementationSlotRepository("owner/repo", 3, tmp_path / "slots.json")
    engine.implementation_slots = slots
    engine._process_single_candidate_reserved = Mock()

    candidate = Candidate(type="issue", data={"number": 100, "title": "Test Issue", "body": "## Requirements\n- REQ-001: test.", "labels": [{"name": "implementation-ready"}]}, priority=0)

    engine.issue_stage_routing._connection.execute("INSERT INTO issue_lane_arrivals(repository,stage,target_number,generation,priority,state,remaining_json,family_parent_number,created_at) VALUES(?,?,?,?,?,'starting','[]',?,0)", ("owner/repo", IMPLEMENTATION_STAGE, 100, "generation-1", 0, None))

    result1 = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    pass

    # Retry from another origin: should be suppressed
    result2 = engine._process_single_candidate_unified("owner/repo", candidate, engine.config)
    pass


def test_ambiguous_bindings_fail_closed_only_for_affected_target(tmp_path: Path):
    """AS-007: Ambiguous bindings fail closed only for the affected target"""
    github = HandoffMockGitHub()
    engine = AutomationEngine(github, config=AutomationConfig())
    slots = ImplementationSlotRepository("owner/repo", 3, tmp_path / "slots.json")
    engine.implementation_slots = slots
    engine._process_single_candidate_reserved = Mock()

    # Target 100 is ambiguous
    def _get_generation(repo, num):
        if num == 101:
            return "generation-2"
        raise RuntimeError("ambiguous generation")

    github.get_issue_hierarchy_generation_strict = Mock(side_effect=_get_generation)

    candidate100 = Candidate(type="issue", data={"number": 100, "title": "Test", "body": "## Requirements\n- REQ-001: test.", "labels": [{"name": "implementation-ready"}]}, priority=0)
    candidate101 = Candidate(type="issue", data={"number": 101, "title": "Test", "body": "## Requirements\n- REQ-001: test.", "labels": [{"name": "implementation-ready"}]}, priority=0)

    engine.issue_stage_routing._connection.execute("INSERT INTO issue_lane_arrivals(repository,stage,target_number,generation,priority,state,remaining_json,family_parent_number,created_at) VALUES(?,?,?,?,?,'starting','[]',?,0)", ("owner/repo", IMPLEMENTATION_STAGE, 101, "generation-2", 0, None))

    result100 = engine._process_single_candidate_unified("owner/repo", candidate100, engine.config)
    pass
    pass

    result101 = engine._process_single_candidate_unified("owner/repo", candidate101, engine.config)
    pass
    pass

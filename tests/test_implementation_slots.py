"""Tests for durable logical implementation ownership."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from auto_coder.implementation_slots import (
    ImplementationOwner,
    ImplementationOwnerResolutionError,
    ImplementationSlotRepository,
)


class GitHubState:
    def __init__(self, issues=None, prs=None):
        self.issues = issues or {}
        self.prs = prs or {}

    def get_issue(self, _repo, number):
        value = self.issues[number]
        if isinstance(value, Exception):
            raise value
        return value

    def get_issue_details(self, issue):
        return issue

    def get_pull_request(self, _repo, number):
        return self.prs[number]

    def get_pr_details(self, pr):
        return pr


def repository(tmp_path, limit=1):
    return ImplementationSlotRepository("owner/repo", limit, tmp_path / "slots.json")


def test_reservation_is_durable_and_independent_owners_obey_limit(tmp_path):
    first = repository(tmp_path)
    assert first.reserve(ImplementationOwner("issue", 100)) is True

    after_restart = repository(tmp_path)
    assert after_restart.active_owners() == (ImplementationOwner("issue", 100),)
    assert after_restart.reserve(ImplementationOwner("issue", 200)) is False


def test_related_pr_reuses_issue_owner_and_multiple_prs_count_once(tmp_path):
    github = GitHubState(issues={100: {"number": 100, "state": "open"}})
    slots = repository(tmp_path)
    issue = slots.resolve_owner("issue", {"number": 100}, github)
    first_pr = slots.resolve_owner("pr", {"number": 105, "body": "Closes #100"}, github)
    second_pr = slots.resolve_owner("pr", {"number": 108, "body": "Fixes #100"}, github)

    assert issue == first_pr == second_pr == ImplementationOwner("issue", 100)
    assert slots.reserve(issue) is True
    assert slots.reserve(first_pr) is True
    assert slots.active_owners() == (issue,)


def test_standalone_pr_has_independent_owner(tmp_path):
    slots = repository(tmp_path)
    github = GitHubState()
    assert slots.reserve(ImplementationOwner("issue", 100)) is True
    owner = slots.resolve_owner("pr", {"number": 200, "body": "No linked issue"}, github)
    assert owner == ImplementationOwner("pr", 200)
    assert slots.reserve(owner) is False


def test_reconciliation_releases_terminal_but_retains_uncertain_owner(tmp_path):
    slots = repository(tmp_path, limit=2)
    terminal = ImplementationOwner("issue", 100)
    uncertain = ImplementationOwner("issue", 200)
    slots.reserve(terminal)
    slots.reserve(uncertain)
    github = GitHubState(issues={100: {"state": "closed"}, 200: RuntimeError("offline")})

    slots.reconcile(github)

    assert slots.active_owners() == (uncertain,)


def test_pr_resolution_failure_is_not_treated_as_standalone(tmp_path):
    slots = repository(tmp_path)
    github = GitHubState(issues={100: RuntimeError("offline")})
    with pytest.raises(ImplementationOwnerResolutionError, match="offline"):
        slots.resolve_owner("pr", {"number": 105, "body": "Closes #100"}, github)


def test_atomic_reservation_never_exceeds_limit(tmp_path):
    owners = [ImplementationOwner("issue", number) for number in range(10)]

    def reserve(owner):
        return repository(tmp_path).reserve(owner)

    with ThreadPoolExecutor(max_workers=10) as executor:
        outcomes = list(executor.map(reserve, owners))

    assert outcomes.count(True) == 1
    assert len(repository(tmp_path).active_owners()) == 1


def test_configuration_rejects_non_positive_limit(tmp_path):
    with pytest.raises(ValueError, match="positive integer"):
        repository(tmp_path, limit=0)


def test_same_owner_serialization_is_reentrant(tmp_path):
    slots = repository(tmp_path)
    owner = ImplementationOwner("issue", 100)
    with slots.serialize(owner):
        with slots.serialize(owner):
            assert owner.key == "issue:100"

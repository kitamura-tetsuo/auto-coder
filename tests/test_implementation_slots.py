"""Tests for durable logical implementation ownership."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from auto_coder.implementation_slots import (
    ImplementationOwner,
    ImplementationOwnerResolutionError,
    ImplementationSlotRepository,
)
from auto_coder.llm_backend_config import get_max_concurrent_implementations_from_config
from auto_coder.util.gh_cache import GitHubClient


class GitHubState:
    def __init__(self, issues=None, prs=None, linked_prs=None):
        self.issues = issues or {}
        self.prs = prs or {}
        self.linked_prs = linked_prs or {}

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

    def get_linked_prs(self, _repo, issue_number, strict=False):
        return self.linked_prs.get(issue_number, [])


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


def test_closed_issue_retains_slot_while_sibling_pr_is_open(tmp_path):
    slots = repository(tmp_path)
    issue_owner = ImplementationOwner("issue", 100)
    assert slots.reserve(issue_owner) is True
    github = GitHubState(
        issues={100: {"number": 100, "state": "closed"}},
        prs={
            105: {"number": 105, "state": "closed", "merged": True, "body": "Closes #100"},
            108: {"number": 108, "state": "open", "merged": False, "body": "Fixes #100"},
        },
        linked_prs={100: [105, 108]},
    )

    slots.reconcile(github)

    assert slots.active_owners() == (issue_owner,)
    assert slots.reserve(ImplementationOwner("issue", 200)) is False
    sibling_owner = slots.resolve_owner("pr", github.prs[108], github)
    assert sibling_owner == issue_owner
    assert slots.reserve(sibling_owner) is True
    assert slots.active_owners() == (issue_owner,)


def test_reconciliation_retains_slot_when_production_timeline_is_unavailable(tmp_path, monkeypatch):
    slots = repository(tmp_path)
    issue_owner = ImplementationOwner("issue", 100)
    assert slots.reserve(issue_owner) is True
    github = GitHubClient("token")
    monkeypatch.setattr(github, "get_issue", lambda _repo, _number: {"number": 100, "state": "closed"})
    monkeypatch.setattr(github, "get_issue_details", lambda issue: issue)

    class UnavailableTimeline:
        def get(self, _url, headers=None):
            raise RuntimeError("timeline offline")

    monkeypatch.setattr("auto_coder.util.gh_cache.get_caching_client", lambda: UnavailableTimeline())

    slots.reconcile(github)

    assert slots.active_owners() == (issue_owner,)
    assert slots.reserve(ImplementationOwner("issue", 200)) is False


def test_reconciliation_reads_all_timeline_pages_before_releasing_slot(tmp_path, monkeypatch):
    slots = repository(tmp_path)
    issue_owner = ImplementationOwner("issue", 100)
    assert slots.reserve(issue_owner) is True
    github = GitHubClient("token")
    monkeypatch.setattr(github, "get_issue", lambda _repo, _number: {"number": 100, "state": "closed"})
    monkeypatch.setattr(github, "get_issue_details", lambda issue: issue)
    monkeypatch.setattr(
        GitHubClient,
        "get_issue_strict",
        lambda _self, _repo, number: {"number": number, "state": "closed"},
    )
    monkeypatch.setattr(
        github,
        "get_pull_request",
        lambda _repo, number: {"number": number, "state": "open", "merged": False},
    )
    monkeypatch.setattr(github, "get_pr_details", lambda pr: pr)

    first_url = "https://api.github.com/repos/owner/repo/issues/100/timeline?per_page=100"
    second_url = f"{first_url}&page=2"

    class TimelineResponse:
        def __init__(self, events, next_url=None):
            self.events = events
            self.links = {"next": {"url": next_url}} if next_url else {}

        def raise_for_status(self):
            return None

        def json(self):
            return self.events

    class PaginatedTimeline:
        def __init__(self):
            self.requested_urls = []

        def get(self, url, headers=None):
            self.requested_urls.append(url)
            if url == first_url:
                return TimelineResponse([{"event": "commented"}] * 100, second_url)
            assert url == second_url
            return TimelineResponse(
                [
                    {
                        "event": "cross-referenced",
                        "source": {"issue": {"number": 108, "pull_request": {}}},
                    }
                ]
            )

    timeline = PaginatedTimeline()
    monkeypatch.setattr("auto_coder.util.gh_cache.get_caching_client", lambda: timeline)

    slots.reconcile(github)

    assert timeline.requested_urls == [first_url, second_url]
    assert slots.active_owners() == (issue_owner,)
    assert slots.reserve(ImplementationOwner("issue", 200)) is False
    sibling_owner = slots.resolve_owner("pr", {"number": 108, "body": "Fixes #100"}, github)
    assert sibling_owner == issue_owner
    assert slots.reserve(sibling_owner) is True


def test_reconciliation_retains_branch_linked_pr_absent_from_timeline(tmp_path, monkeypatch):
    slots = repository(tmp_path)
    github = GitHubClient("token")
    monkeypatch.setattr(
        GitHubClient,
        "get_issue_strict",
        lambda _self, _repo, number: {"number": number, "state": "closed"},
    )
    monkeypatch.setattr(github, "get_issue", lambda _repo, number: {"number": number, "state": "closed"})
    monkeypatch.setattr(github, "get_issue_details", lambda issue: issue)
    monkeypatch.setattr(
        github,
        "get_pull_request",
        lambda _repo, number: {"number": number, "state": "open", "merged": False},
    )
    monkeypatch.setattr(github, "get_pr_details", lambda pr: pr)

    class EmptyTimelineResponse:
        links = {}

        def raise_for_status(self):
            return None

        def json(self):
            return []

    class EmptyTimeline:
        def get(self, _url, headers=None):
            return EmptyTimelineResponse()

    monkeypatch.setattr("auto_coder.util.gh_cache.get_caching_client", lambda: EmptyTimeline())
    branch_linked_pr = {
        "number": 108,
        "title": "Implementation",
        "body": "",
        "head": {"ref": "issue-100-work"},
    }
    issue_owner = slots.resolve_owner("pr", branch_linked_pr, github)
    assert issue_owner == ImplementationOwner("issue", 100)
    assert slots.reserve(issue_owner, implementation_pr=108) is True

    restarted_slots = repository(tmp_path)
    restarted_slots.reconcile(github)

    assert restarted_slots.active_owners() == (issue_owner,)
    assert restarted_slots.reserve(ImplementationOwner("issue", 200)) is False
    assert restarted_slots.resolve_owner("pr", branch_linked_pr, github) == issue_owner
    assert restarted_slots.reserve(issue_owner, implementation_pr=108) is True


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


@pytest.mark.parametrize("invalid_value", ["true", "1.5", '"1"'])
def test_configuration_rejects_non_integer_toml_values(tmp_path, invalid_value):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"[process_issues]\nmax_concurrent_implementations = {invalid_value}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="positive integer"):
        get_max_concurrent_implementations_from_config(str(config_path))


def test_same_owner_serialization_is_reentrant(tmp_path):
    slots = repository(tmp_path)
    owner = ImplementationOwner("issue", 100)
    with slots.serialize(owner):
        with slots.serialize(owner):
            assert owner.key == "issue:100"

"""Regression tests for Issue #2270: recover a Claude source-Issue association when a
PR and its originating Issue use opposite `session_`/`cse_` session-ID prefixes.

`_find_issue_by_session_id_in_comments` is the GitHub-search fallback used when no
durable local/session association exists. Before this fix its GitHub Issue search
only queried the exact spelling extracted from the PR body, so an Issue recorded
under the opposite Claude Routine spelling (`cse_<S>` vs `session_<S>`) was never
discovered even though the two spellings are lookup-equivalent.
"""

from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.cloud_manager import CloudManager, claude_session_alias
from src.auto_coder.issue_processor import _process_issue_claude_routine_mode
from src.auto_coder.pr_processor import _find_issue_by_session_id_in_comments, _link_jules_pr_to_issue


class _FakePull:
    def __init__(self, client: "FakeGitHubClient", pr_number: int) -> None:
        self._client = client
        self._pr_number = pr_number

    def edit(self, body: str) -> None:
        if self._pr_number in self._client.reject_update_for:
            self._client.reject_update_for.discard(self._pr_number)
            raise RuntimeError("simulated PR update rejection")
        self._client.pr_bodies[self._pr_number] = body
        self._client.pr_update_calls.append(body)


class _FakeRepo:
    def __init__(self, client: "FakeGitHubClient") -> None:
        self._client = client

    def get_pull(self, pr_number: int) -> _FakePull:
        return _FakePull(self._client, pr_number)


class FakeGitHubClient:
    """Controlled GitHub boundary double.

    Search results and failures are keyed by the exact spelling that must appear in
    the query, so discovery for one spelling can be made to fail or return loose
    hits independently of the other. Issue reads and comment reads are similarly
    controllable per issue number, and PR-body updates are recorded (or can be made
    to fail once) via `get_repository(...).get_pull(...).edit(...)`.
    """

    def __init__(self) -> None:
        self.search_results: Dict[str, List[Dict[str, Any]]] = {}
        self.search_failures: set = set()
        self.issues: Dict[int, Dict[str, Any]] = {}
        self.issue_read_failures: set = set()
        self.comments: Dict[int, List[Dict[str, Any]]] = {}
        self.comment_read_failures: set = set()
        self.search_calls: List[str] = []
        self.pr_bodies: Dict[int, str] = {}
        self.pr_update_calls: List[str] = []
        self.reject_update_for: set = set()

    def search_issues_strict(self, query: str, sort: str = "updated", order: str = "desc") -> List[Dict[str, Any]]:
        self.search_calls.append(query)
        for spelling, results in self.search_results.items():
            if spelling in query:
                if spelling in self.search_failures:
                    raise RuntimeError(f"search transport failure for '{spelling}'")
                return results
        return []

    def get_issue_strict(self, repo_name: str, issue_number: int) -> Dict[str, Any]:
        if issue_number in self.issue_read_failures:
            raise RuntimeError(f"issue read failure for #{issue_number}")
        return self.issues[issue_number]

    def get_issue(self, repo_name: str, issue_number: int) -> Any:
        try:
            return self.get_issue_strict(repo_name, issue_number)
        except Exception:
            return None

    def get_issue_comments_strict(self, repo_name: str, issue_number: int) -> List[Dict[str, Any]]:
        if issue_number in self.comment_read_failures:
            raise RuntimeError(f"comment read failure for #{issue_number}")
        return self.comments.get(issue_number, [])

    def get_repository(self, repo_name: str) -> _FakeRepo:
        return _FakeRepo(self)


class TestClaudeSessionAliasHelper:
    def test_boundary_conditions(self) -> None:
        assert claude_session_alias("session_abc-123_XY") == "cse_abc-123_XY"
        assert claude_session_alias("cse_abc-123_XY") == "session_abc-123_XY"
        assert claude_session_alias("session_") is None
        assert claude_session_alias("cse_") is None
        assert claude_session_alias("sessionfoo") is None
        assert claude_session_alias("task_abc123") is None


class TestOppositePrefixRecovery:
    """AS-001: real Claude start comment, missing local mapping, opposite prefix."""

    def test_recovers_via_real_start_comment_with_opposite_prefix(self, tmp_path) -> None:
        repo_name = "owner/repo"
        issue_number = 5314
        suffix = "01RrztBJsi7yugKx4qdxBszD"
        cse_id = f"cse_{suffix}"
        session_id_alt = f"session_{suffix}"
        session_url = f"https://claude.ai/code/{cse_id}"

        issue_data = {"number": issue_number, "title": "Some fix", "body": "Details", "labels": [], "state": "open"}
        github_for_issue = MagicMock()
        captured_comments: Dict[int, List[str]] = {}
        github_for_issue.add_comment_to_issue.side_effect = lambda _repo, num, body: captured_comments.setdefault(num, []).append(body)

        # Phase 1: produce the real Claude Routine Issue-start comment via the
        # production dispatch path, backed by a first, throwaway local install.
        manager_a = CloudManager(repo_name, cloud_file_path=tmp_path / "install-a" / "cloud.csv")
        with (
            patch("src.auto_coder.issue_processor.CloudManager", return_value=manager_a),
            patch("src.auto_coder.claude_routine_client.ClaudeRoutineClient.fire_routine", return_value=(cse_id, session_url)),
            patch("src.auto_coder.issue_processor.get_commit_log", return_value="initial"),
        ):
            _process_issue_claude_routine_mode(repo_name, issue_data, AutomationConfig(), github_for_issue, backend_name="claude-routine")

        start_comment = captured_comments[issue_number][0]
        assert f"Session ID: {cse_id}" in start_comment
        assert session_url in start_comment

        # Phase 2: a PR-processing evaluation from a fresh/recovered installation
        # (no local session mapping), with the opposite session-ID spelling in the
        # PR body and search that only finds the Issue under the Claude prefix.
        pr_data = {
            "number": 9001,
            "title": "Implement the reported fix",
            "body": f"Fixes the reported bug.\n\nhttps://claude.ai/code/{session_id_alt}",
            "user": {"login": "kitamura-tetsuo"},
            "head": {"ref": "claude/some-branch"},
        }

        fake_client = FakeGitHubClient()
        fake_client.search_results[session_id_alt] = []  # the PR's own spelling: zero results
        fake_client.search_results[cse_id] = [{"number": issue_number}]  # the alternate spelling finds it
        fake_client.issues[issue_number] = {"number": issue_number, "body": issue_data["body"]}
        fake_client.comments[issue_number] = [{"body": start_comment}]

        manager_b = CloudManager(repo_name, cloud_file_path=tmp_path / "install-b" / "cloud.csv")
        with patch("src.auto_coder.pr_processor.CloudManager", return_value=manager_b):
            linked = _link_jules_pr_to_issue(repo_name, pr_data, fake_client)

        assert linked is True
        assert f"close #{issue_number}" in pr_data["body"]
        assert f"https://github.com/{repo_name}/issues/{issue_number}" in pr_data["body"]
        assert fake_client.pr_bodies[9001] == pr_data["body"]

        # Both spellings were actually searched; the missing spelling did not
        # short-circuit discovery of the alternate one.
        assert any(session_id_alt in q for q in fake_client.search_calls)
        assert any(cse_id in q for q in fake_client.search_calls)

        # Reprocessing the successfully linked body performs no extra link update.
        fake_client.pr_update_calls.clear()
        with patch("src.auto_coder.pr_processor.CloudManager", return_value=manager_b):
            linked_again = _link_jules_pr_to_issue(repo_name, pr_data, fake_client)
        assert linked_again is True
        assert fake_client.pr_update_calls == []
        assert pr_data["body"].count(f"close #{issue_number}") == 1


class TestReverseDirectionAndDuplicates:
    """AS-002: reverse direction, same-spelling success, and duplicate discovery."""

    def test_reverse_direction_with_duplicate_search_hits(self, tmp_path) -> None:
        repo_name = "owner/repo"
        issue_number = 4200
        suffix = "abcDEF123"
        cse_id = f"cse_{suffix}"
        session_id = f"session_{suffix}"

        pr_data = {
            "number": 7100,
            "title": "Fix thing",
            "body": f"Work done.\n\nhttps://claude.ai/code/{cse_id}",
            "user": {"login": "kitamura-tetsuo"},
            "head": {"ref": "claude/branch-x"},
        }

        fake_client = FakeGitHubClient()
        fake_client.search_results[cse_id] = []  # PR's own spelling finds nothing
        # Alternate spelling repeats the same Issue: it must still count once.
        fake_client.search_results[session_id] = [{"number": issue_number}, {"number": issue_number}]
        fake_client.issues[issue_number] = {"number": issue_number, "body": f"Tracking {session_id} work"}

        manager = CloudManager(repo_name, cloud_file_path=tmp_path / "cloud.csv")
        with patch("src.auto_coder.pr_processor.CloudManager", return_value=manager):
            linked = _link_jules_pr_to_issue(repo_name, pr_data, fake_client)

        assert linked is True
        assert f"close #{issue_number}" in pr_data["body"]


class TestSearchBudgetIsPerSpelling:
    """AS-003: discovery budget cannot starve the alternate spelling."""

    def test_five_loose_hits_for_own_spelling_do_not_block_alternate(self) -> None:
        repo_name = "owner/repo"
        issue_number = 3100
        suffix = "budgetTest001"
        own_spelling = f"session_{suffix}"
        alt_spelling = f"cse_{suffix}"

        fake_client = FakeGitHubClient()
        loose_hits = [{"number": 9000 + i, "body": "unrelated content"} for i in range(5)]
        fake_client.search_results[own_spelling] = loose_hits
        for hit in loose_hits:
            fake_client.issues[hit["number"]] = hit
            fake_client.comments[hit["number"]] = []
        fake_client.search_results[alt_spelling] = [{"number": issue_number}]
        fake_client.issues[issue_number] = {"number": issue_number, "body": f"See {alt_spelling}"}

        result = _find_issue_by_session_id_in_comments(repo_name, own_spelling, fake_client)

        assert result == issue_number


class TestMisleadingHitsAreRejected:
    """AS-004: near-miss search hits must not authorize a link."""

    def test_title_only_longer_token_unrelated_and_pr_hits_are_all_rejected(self) -> None:
        repo_name = "owner/repo"
        suffix = "MixCase007"
        own_spelling = f"session_{suffix}"
        alt_spelling = f"cse_{suffix}"

        candidates = [
            {"number": 1, "title": f"mentions {own_spelling} only in the title", "body": "no token here"},
            {"number": 2, "body": f"contains {own_spelling}Z, a longer token"},
            {"number": 3, "body": "totally unrelated issue"},
            # Exact qualifying token, but the read reveals this is actually a PR.
            {"number": 4, "body": f"looks relevant: {own_spelling}", "pull_request": {"url": "https://example/pulls/4"}},
        ]

        fake_client = FakeGitHubClient()
        fake_client.search_results[own_spelling] = candidates
        fake_client.search_results[alt_spelling] = []
        for candidate in candidates:
            fake_client.issues[candidate["number"]] = candidate
            fake_client.comments[candidate["number"]] = []

        result = _find_issue_by_session_id_in_comments(repo_name, own_spelling, fake_client)

        assert result is None

    def test_special_title_exemption_still_applies(self, tmp_path) -> None:
        repo_name = "owner/repo"
        suffix = "ExemptCase1"
        pr_data = {
            "number": 8800,
            "title": "🛡️ Sentinel: routine sweep",
            "body": f"https://claude.ai/code/cse_{suffix}",
            "user": {"login": "claude[bot]"},
            "head": {"ref": "claude/exempt"},
        }
        original_body = pr_data["body"]
        fake_client = FakeGitHubClient()
        fake_client.search_results[f"session_{suffix}"] = [{"number": 42}]
        fake_client.issues[42] = {"number": 42, "body": f"session_{suffix}"}

        manager = CloudManager(repo_name, cloud_file_path=tmp_path / "cloud.csv")
        with patch("src.auto_coder.pr_processor.CloudManager", return_value=manager):
            linked = _link_jules_pr_to_issue(repo_name, pr_data, fake_client)

        assert linked is True  # exempt: treated as "nothing to do", not an error
        assert pr_data["body"] == original_body
        assert fake_client.search_calls == []


class TestAmbiguityAndLocalPrecedence:
    """AS-005: duplicate evidence versus conflicting owners."""

    def test_two_distinct_verified_issues_are_ambiguous(self) -> None:
        repo_name = "owner/repo"
        suffix = "AmbiguousXYZ"
        own_spelling = f"session_{suffix}"
        alt_spelling = f"cse_{suffix}"

        fake_client = FakeGitHubClient()
        fake_client.search_results[own_spelling] = [{"number": 501}]
        fake_client.search_results[alt_spelling] = [{"number": 502}]
        fake_client.issues[501] = {"number": 501, "body": own_spelling}
        fake_client.issues[502] = {"number": 502, "body": alt_spelling}

        assert _find_issue_by_session_id_in_comments(repo_name, own_spelling, fake_client) is None

    def test_repeated_hits_for_one_issue_are_not_ambiguous(self) -> None:
        repo_name = "owner/repo"
        suffix = "NotAmbiguous1"
        own_spelling = f"session_{suffix}"
        alt_spelling = f"cse_{suffix}"

        fake_client = FakeGitHubClient()
        fake_client.search_results[own_spelling] = [{"number": 501}]
        fake_client.search_results[alt_spelling] = [{"number": 501}]
        fake_client.issues[501] = {"number": 501, "body": f"{own_spelling} and {alt_spelling}"}

        assert _find_issue_by_session_id_in_comments(repo_name, own_spelling, fake_client) == 501

    def test_ambiguous_alias_search_does_not_fall_back_to_title_or_branch_guess(self, tmp_path) -> None:
        """REQ-003: an ambiguous alias search must not let a guessable PR title or
        branch publish a link for that evaluation.

        Production entry point: _link_jules_pr_to_issue -> _resolve_jules_pr_issue_number.
        Without this, an ambiguous _find_issue_by_session_id_in_comments result was
        indistinguishable from "not found", so the resolver fell through to the
        branch/title fallback and could still publish a guessed link.
        """
        repo_name = "owner/repo"
        suffix = "AmbigGuess1"
        own_spelling = f"session_{suffix}"
        alt_spelling = f"cse_{suffix}"

        fake_client = FakeGitHubClient()
        fake_client.search_results[own_spelling] = [{"number": 501}]
        fake_client.search_results[alt_spelling] = [{"number": 502}]
        fake_client.issues[501] = {"number": 501, "body": own_spelling}
        fake_client.issues[502] = {"number": 502, "body": alt_spelling}

        # Both the PR title and the branch name contain a guessable Issue number
        # (#501), matching the Issue this alias search happens to have discovered
        # first -- exactly the guess the fallback must not make.
        pr_data = {
            "number": 9100,
            "title": "Fix #501",
            "body": f"https://claude.ai/code/{own_spelling}",
            "user": {"login": "kitamura-tetsuo"},
            "head": {"ref": "claude/issue-501"},
        }
        original_body = pr_data["body"]

        manager = CloudManager(repo_name, cloud_file_path=tmp_path / "cloud.csv")
        with patch("src.auto_coder.pr_processor.CloudManager", return_value=manager):
            linked = _link_jules_pr_to_issue(repo_name, pr_data, fake_client)

        assert linked is False
        assert pr_data["body"] == original_body
        assert 9100 not in fake_client.pr_bodies

    def test_unique_local_association_is_not_displaced_by_search(self, tmp_path) -> None:
        repo_name = "owner/repo"
        suffix = "LocalWins1"
        session_id = f"session_{suffix}"

        manager = CloudManager(repo_name, cloud_file_path=tmp_path / "cloud.csv")
        manager.add_session(777, f"cse_{suffix}", provider="claude-routine", backend_name="claude-sonnet-routine")

        pr_data = {
            "number": 6600,
            "title": "Fix",
            "body": f"https://claude.ai/code/{session_id}",
            "user": {"login": "kitamura-tetsuo"},
            "head": {"ref": "claude/local-wins"},
        }

        fake_client = FakeGitHubClient()
        fake_client.search_failures.add(session_id)  # would blow up if search were even attempted
        fake_client.search_results[session_id] = []

        with patch("src.auto_coder.pr_processor.CloudManager", return_value=manager):
            linked = _link_jules_pr_to_issue(repo_name, pr_data, fake_client)

        assert linked is True
        assert "close #777" in pr_data["body"]
        assert fake_client.search_calls == []

    def test_competing_local_associations_are_not_resolved_via_search(self, tmp_path) -> None:
        repo_name = "owner/repo"
        suffix = "Competing01"
        session_id = f"session_{suffix}"

        manager = CloudManager(repo_name, cloud_file_path=tmp_path / "cloud.csv")
        manager.add_session(101, f"cse_{suffix}", provider="claude-routine", backend_name="claude-sonnet-routine")
        manager.add_session(102, session_id, provider="claude-routine", backend_name="claude-sonnet-routine")

        pr_data = {
            "number": 6601,
            "title": "Fix",
            "body": f"https://claude.ai/code/{session_id}",
            "user": {"login": "kitamura-tetsuo"},
            "head": {"ref": "claude/competing"},
        }
        original_body = pr_data["body"]

        fake_client = FakeGitHubClient()
        # A search "winner" that must NOT be used to arbitrarily break the tie.
        fake_client.search_results[session_id] = [{"number": 999}]
        fake_client.issues[999] = {"number": 999, "body": session_id}

        with patch("src.auto_coder.pr_processor.CloudManager", return_value=manager):
            linked = _link_jules_pr_to_issue(repo_name, pr_data, fake_client)

        assert linked is False
        assert pr_data["body"] == original_body
        assert fake_client.search_calls == []


class TestPartialObservationsAndUpdateFailure:
    """AS-006: partial observation, later recovery, and update failure."""

    def test_one_spelling_search_failure_refuses_then_later_recovers(self) -> None:
        repo_name = "owner/repo"
        issue_number = 3300
        suffix = "PartialFail1"
        own_spelling = f"session_{suffix}"
        alt_spelling = f"cse_{suffix}"

        fake_client = FakeGitHubClient()
        fake_client.search_results[own_spelling] = []
        fake_client.search_failures.add(alt_spelling)
        fake_client.search_results[alt_spelling] = [{"number": issue_number}]

        assert _find_issue_by_session_id_in_comments(repo_name, own_spelling, fake_client) is None

        # Later evaluation over the SAME session: both spelling searches now succeed.
        fake_client.search_failures.discard(alt_spelling)
        fake_client.issues[issue_number] = {"number": issue_number, "body": alt_spelling}

        assert _find_issue_by_session_id_in_comments(repo_name, own_spelling, fake_client) == issue_number

    def test_candidate_read_failure_refuses_then_later_recovers(self) -> None:
        repo_name = "owner/repo"
        issue_number = 3400
        suffix = "ReadFail1"
        own_spelling = f"session_{suffix}"

        fake_client = FakeGitHubClient()
        fake_client.search_results[own_spelling] = [{"number": issue_number}]
        fake_client.issue_read_failures.add(issue_number)

        assert _find_issue_by_session_id_in_comments(repo_name, own_spelling, fake_client) is None

        fake_client.issue_read_failures.discard(issue_number)
        fake_client.issues[issue_number] = {"number": issue_number, "body": own_spelling}

        assert _find_issue_by_session_id_in_comments(repo_name, own_spelling, fake_client) == issue_number

    def test_pr_body_update_rejection_then_later_success_is_idempotent(self, tmp_path) -> None:
        repo_name = "owner/repo"
        issue_number = 3500
        suffix = "UpdateFail1"
        session_id = f"session_{suffix}"

        pr_data = {
            "number": 7700,
            "title": "Fix",
            "body": f"https://claude.ai/code/{session_id}",
            "user": {"login": "kitamura-tetsuo"},
            "head": {"ref": "claude/update-fail"},
        }
        original_body = pr_data["body"]

        fake_client = FakeGitHubClient()
        fake_client.search_results[session_id] = [{"number": issue_number}]
        fake_client.issues[issue_number] = {"number": issue_number, "body": session_id}
        fake_client.reject_update_for.add(7700)

        manager = CloudManager(repo_name, cloud_file_path=tmp_path / "cloud.csv")
        with patch("src.auto_coder.pr_processor.CloudManager", return_value=manager):
            linked = _link_jules_pr_to_issue(repo_name, pr_data, fake_client)

        assert linked is False
        assert pr_data["body"] == original_body
        assert 7700 not in fake_client.pr_bodies

        with patch("src.auto_coder.pr_processor.CloudManager", return_value=manager):
            linked_again = _link_jules_pr_to_issue(repo_name, pr_data, fake_client)

        assert linked_again is True
        assert f"close #{issue_number}" in pr_data["body"]
        assert fake_client.pr_bodies[7700] == pr_data["body"]

        # Idempotent repetition: no duplicate reference or extra update.
        fake_client.pr_update_calls.clear()
        with patch("src.auto_coder.pr_processor.CloudManager", return_value=manager):
            linked_third = _link_jules_pr_to_issue(repo_name, pr_data, fake_client)
        assert linked_third is True
        assert fake_client.pr_update_calls == []
        assert pr_data["body"].count(f"close #{issue_number}") == 1

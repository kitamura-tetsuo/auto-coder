from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.auto_coder.util.gh_cache import GitHubClient, PartialPRChangedFilesError
from src.auto_coder.util.github_request_outcome import (
    DeliveryCertainty,
    GitHubApiOutcome,
    GitHubRequestContext,
    GitHubRequestError,
    GitHubRequestOutcome,
    GitHubResponseMetadata,
    RequestProvenance,
)


def _github_request_error(classification: GitHubApiOutcome) -> GitHubRequestError:
    """Build a minimal GitHubRequestError as the production diagnostic
    boundary (DiagnosticTransport) would raise it for a given classification."""
    context = GitHubRequestContext("op-1", "attempt-1", "ghapi", "https://api.github.com", "GET", "read", "/repos/{owner}/{repo}/pulls/{id}/files")
    outcome = GitHubRequestOutcome(context, None, classification, RequestProvenance.NETWORK, DeliveryCertainty.INDETERMINATE, GitHubResponseMetadata(), 1.0)
    return GitHubRequestError(outcome)


class TestGetPrChangedFilesPagination:
    """REQ-002/REQ-008: successful per-path evidence must survive a later
    pagination failure instead of being discarded alongside it, and a
    transient failure on one page must not force re-fetching pages that
    already succeeded."""

    def _page_records(self, count: int, prefix: str) -> list:
        return [{"filename": f"{prefix}_{index}.py", "additions": 1, "deletions": 0, "patch": f"+{prefix}_{index}"} for index in range(count)]

    def test_persistent_page_two_failure_retains_page_one_records(self):
        client = GitHubClient(token="secret-token")
        page_one = self._page_records(100, "page1")

        mock_api = MagicMock()
        mock_api.pulls.list_files.side_effect = [
            page_one,
            httpx.ConnectError("network unreachable"),
            httpx.ConnectError("network unreachable"),
            httpx.ConnectError("network unreachable"),
            httpx.ConnectError("network unreachable"),
        ]

        with (
            patch("src.auto_coder.util.gh_cache.get_ghapi_client", return_value=mock_api),
            patch("src.auto_coder.util.gh_cache.time.sleep"),
        ):
            with pytest.raises(PartialPRChangedFilesError) as excinfo:
                client.get_pr_changed_files("owner/repo", 101)

        error = excinfo.value
        assert error.failed_page == 2
        assert [record["filename"] for record in error.partial_records] == [record["filename"] for record in page_one]

        # Page one must be fetched exactly once: retries apply only to the
        # page that actually failed, never a full pagination restart.
        page_one_calls = [call for call in mock_api.pulls.list_files.call_args_list if call.kwargs.get("page") == 1]
        assert len(page_one_calls) == 1

    def test_transient_single_page_failure_recovers_without_losing_prior_pages(self):
        client = GitHubClient(token="secret-token")
        page_one = self._page_records(100, "page1")
        page_two = self._page_records(1, "page2")

        mock_api = MagicMock()
        mock_api.pulls.list_files.side_effect = [
            page_one,
            httpx.ConnectError("transient"),
            page_two,
        ]

        with (
            patch("src.auto_coder.util.gh_cache.get_ghapi_client", return_value=mock_api),
            patch("src.auto_coder.util.gh_cache.time.sleep"),
        ):
            records = client.get_pr_changed_files("owner/repo", 101)

        assert [record["filename"] for record in records] == [record["filename"] for record in page_one] + [record["filename"] for record in page_two]
        page_one_calls = [call for call in mock_api.pulls.list_files.call_args_list if call.kwargs.get("page") == 1]
        assert len(page_one_calls) == 1

    def test_production_transport_failure_error_type_is_also_treated_as_partial_recoverable(self):
        """REQ-002/REQ-008: the production diagnostic boundary (CachedGhApi /
        DiagnosticTransport) wraps a raw transport exception in
        GitHubRequestError(TRANSPORT_FAILURE), not a bare httpx exception.
        That typed error must also trigger partial recovery, or page-one
        evidence is lost whenever this production path (rather than a raw
        httpx exception) is what actually fails."""
        client = GitHubClient(token="secret-token")
        page_one = self._page_records(100, "page1")
        transport_failure = _github_request_error(GitHubApiOutcome.TRANSPORT_FAILURE)

        mock_api = MagicMock()
        mock_api.pulls.list_files.side_effect = [page_one, transport_failure, transport_failure, transport_failure, transport_failure]

        with (
            patch("src.auto_coder.util.gh_cache.get_ghapi_client", return_value=mock_api),
            patch("src.auto_coder.util.gh_cache.time.sleep"),
        ):
            with pytest.raises(PartialPRChangedFilesError) as excinfo:
                client.get_pr_changed_files("owner/repo", 101)

        assert [record["filename"] for record in excinfo.value.partial_records] == [record["filename"] for record in page_one]

    def test_non_transient_github_request_error_is_not_masked_as_partial_recovery(self):
        """A permanent API-level rejection (e.g. forbidden) must propagate
        immediately rather than being retried or downgraded to a partial
        recovery: only a genuine transport failure is transient."""
        client = GitHubClient(token="secret-token")
        page_one = self._page_records(100, "page1")
        forbidden = _github_request_error(GitHubApiOutcome.FORBIDDEN)

        mock_api = MagicMock()
        mock_api.pulls.list_files.side_effect = [page_one, forbidden]

        with (
            patch("src.auto_coder.util.gh_cache.get_ghapi_client", return_value=mock_api),
            patch("src.auto_coder.util.gh_cache.time.sleep"),
        ):
            with pytest.raises(GitHubRequestError):
                client.get_pr_changed_files("owner/repo", 101)

        # No retry should have been attempted for a non-transient classification.
        assert mock_api.pulls.list_files.call_count == 2

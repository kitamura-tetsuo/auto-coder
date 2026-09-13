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

    def test_non_transient_github_request_error_still_preserves_prior_records_without_retry(self):
        """REQ-002/REQ-008: a permanent API-level rejection (e.g. an HTTP 500
        surfaced as REMOTE_ERROR) is not worth retrying, but page-one evidence
        already retrieved must still be preserved and delivered alongside the
        failure rather than discarded -- retry and preservation are separate
        questions."""
        client = GitHubClient(token="secret-token")
        page_one = self._page_records(100, "page1")
        remote_error = _github_request_error(GitHubApiOutcome.REMOTE_ERROR)

        mock_api = MagicMock()
        mock_api.pulls.list_files.side_effect = [page_one, remote_error]

        with (
            patch("src.auto_coder.util.gh_cache.get_ghapi_client", return_value=mock_api),
            patch("src.auto_coder.util.gh_cache.time.sleep"),
        ):
            with pytest.raises(PartialPRChangedFilesError) as excinfo:
                client.get_pr_changed_files("owner/repo", 101)

        assert [record["filename"] for record in excinfo.value.partial_records] == [record["filename"] for record in page_one]
        # No retry should have been attempted for a non-transient classification.
        assert mock_api.pulls.list_files.call_count == 2

    def test_unrecognized_exception_propagates_untouched(self):
        """An exception unrelated to the GitHub request boundary (a genuine
        bug, not a retrieval failure) must not be silently downgraded to a
        partial-recovery result."""
        client = GitHubClient(token="secret-token")
        page_one = self._page_records(100, "page1")

        mock_api = MagicMock()
        mock_api.pulls.list_files.side_effect = [page_one, ValueError("unexpected bug")]

        with (
            patch("src.auto_coder.util.gh_cache.get_ghapi_client", return_value=mock_api),
            patch("src.auto_coder.util.gh_cache.time.sleep"),
        ):
            with pytest.raises(ValueError):
                client.get_pr_changed_files("owner/repo", 101)

        assert mock_api.pulls.list_files.call_count == 2

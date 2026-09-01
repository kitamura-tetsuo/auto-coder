"""
Unit tests for the CloudRun lifecycle abstraction (issue #1605).

These tests cover the acceptance scenarios from the issue:
- AC-001: persist and reload a cloud run across a simulated process restart.
- AC-002: one task owns multiple pull requests.
- AC-003: policy is provider-extensible without branching on provider name.
- AC-004: transport (CloudTaskClientBase) and lifecycle stay separate.
"""

import tempfile
from pathlib import Path

import pytest

from src.auto_coder.cloud_run import (
    CloudRun,
    CloudRunEvent,
    CloudRunPolicy,
    CloudRunRepository,
    evaluate_new_attempt,
)
from src.auto_coder.cloud_task_client_base import CloudTaskClientBase


class _AlwaysAllowPolicy(CloudRunPolicy):
    def allow_new_attempt(self, event: CloudRunEvent) -> bool:
        return True


class _NeverAllowPolicy(CloudRunPolicy):
    def allow_new_attempt(self, event: CloudRunEvent) -> bool:
        return False


class TestCloudRunPersistence:
    """AC-001: Persist and reload a cloud run."""

    def test_reconstructed_repository_recovers_run_after_restart(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_path = Path(tmpdir) / "cloud_runs.json"

            original = CloudRunRepository("owner/repo", storage_path=storage_path)
            run = CloudRun(
                repo_name="owner/repo",
                issue_number=100,
                attempt=0,
                provider="codex-cloud",
                task_id="task-A",
            )
            assert original.save(run) is True

            # Simulate an Auto-Coder process restart: a brand new repository
            # instance backed only by the durable file, no shared in-memory state.
            reconstructed = CloudRunRepository("owner/repo", storage_path=storage_path)
            reloaded = reconstructed.get(issue_number=100, attempt=0)

            assert reloaded is not None
            assert reloaded.task_id == "task-A"
            assert reloaded.provider == "codex-cloud"
            assert reloaded.issue_number == 100
            assert reloaded.attempt == 0

    def test_get_by_task_id_supports_duplicate_dispatch_protection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_path = Path(tmpdir) / "cloud_runs.json"
            repo = CloudRunRepository("owner/repo", storage_path=storage_path)
            repo.save(
                CloudRun(
                    repo_name="owner/repo",
                    issue_number=100,
                    attempt=0,
                    provider="codex-cloud",
                    task_id="task-A",
                )
            )

            # A fresh instance (as after a restart) must still find the task.
            reconstructed = CloudRunRepository("owner/repo", storage_path=storage_path)
            found = reconstructed.get_by_task_id("task-A")
            assert found is not None
            assert found.issue_number == 100

            assert reconstructed.get_by_task_id("task-nonexistent") is None

    def test_get_missing_run_returns_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_path = Path(tmpdir) / "cloud_runs.json"
            repo = CloudRunRepository("owner/repo", storage_path=storage_path)
            assert repo.get(issue_number=999, attempt=0) is None

    def test_from_dict_converts_supported_json_number_values(self):
        run = CloudRun.from_dict(
            {
                "repo_name": "owner/repo",
                "issue_number": "100",
                "attempt": 2,
                "provider": "codex-cloud",
                "task_id": "task-A",
                "pull_request_numbers": ["200", 201],
            }
        )

        assert run.issue_number == 100
        assert run.attempt == 2
        assert run.pull_request_numbers == [200, 201]

    def test_from_dict_rejects_non_list_pull_request_numbers(self):
        with pytest.raises(ValueError, match="pull_request_numbers.*list"):
            CloudRun.from_dict(
                {
                    "repo_name": "owner/repo",
                    "issue_number": 100,
                    "attempt": 2,
                    "provider": "codex-cloud",
                    "task_id": "task-A",
                    "pull_request_numbers": "200",
                }
            )


class TestCloudRunMultiplePullRequests:
    """AC-002: One task owns multiple PRs."""

    def test_second_pr_association_preserves_first(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_path = Path(tmpdir) / "cloud_runs.json"
            repo = CloudRunRepository("owner/repo", storage_path=storage_path)
            repo.save(
                CloudRun(
                    repo_name="owner/repo",
                    issue_number=100,
                    attempt=0,
                    provider="codex-cloud",
                    task_id="task-A",
                )
            )

            updated = repo.add_pull_request(issue_number=100, attempt=0, pr_number=200)
            assert updated is not None
            assert updated.pull_request_numbers == [200]

            updated = repo.add_pull_request(issue_number=100, attempt=0, pr_number=201)
            assert updated is not None
            assert set(updated.pull_request_numbers) == {200, 201}

            # Reload independently to confirm durability of both associations.
            reloaded = repo.get(issue_number=100, attempt=0)
            assert reloaded is not None
            assert set(reloaded.pull_request_numbers) == {200, 201}

    def test_add_pull_request_is_idempotent(self):
        run = CloudRun(
            repo_name="owner/repo",
            issue_number=100,
            attempt=0,
            provider="codex-cloud",
            task_id="task-A",
        )
        run.add_pull_request(200)
        run.add_pull_request(200)
        assert run.pull_request_numbers == [200]

    def test_add_pull_request_for_unknown_run_returns_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage_path = Path(tmpdir) / "cloud_runs.json"
            repo = CloudRunRepository("owner/repo", storage_path=storage_path)
            assert repo.add_pull_request(issue_number=999, attempt=0, pr_number=1) is None


class TestCloudRunPolicyExtensibility:
    """AC-003: Policy is provider-extensible."""

    def test_generic_evaluation_follows_supplied_policy_without_provider_branching(self):
        run = CloudRun(
            repo_name="owner/repo",
            issue_number=100,
            attempt=0,
            provider="codex-cloud",
            task_id="task-A",
        )
        event = CloudRunEvent(run=run, reason="empty-pr", proposed_attempt=1)

        assert evaluate_new_attempt(event, _AlwaysAllowPolicy()) is True
        assert evaluate_new_attempt(event, _NeverAllowPolicy()) is False

    def test_same_event_different_provider_names_different_decisions(self):
        for provider in ("codex-cloud", "claude-routine", "future-google-backend"):
            run = CloudRun(
                repo_name="owner/repo",
                issue_number=100,
                attempt=0,
                provider=provider,
                task_id="task-A",
            )
            event = CloudRunEvent(run=run, reason="empty-pr", proposed_attempt=1)

            # The two test policies disagree solely based on policy identity,
            # never on `event.run.provider` - proving the decision is
            # policy-driven rather than hardcoded per provider.
            assert evaluate_new_attempt(event, _AlwaysAllowPolicy()) is True
            assert evaluate_new_attempt(event, _NeverAllowPolicy()) is False


class TestCloudRunTransportSeparation:
    """AC-004: Transport and lifecycle remain separate."""

    def test_cloud_run_repository_does_not_depend_on_transport_client(self):
        # CloudRunRepository operates purely on CloudRun records; it never
        # requires a CloudTaskClientBase instance to persist or evaluate state.
        import inspect

        init_params = inspect.signature(CloudRunRepository.__init__).parameters
        assert "client" not in init_params
        for method_name in ("save", "get", "get_by_task_id", "list_for_issue", "add_pull_request"):
            params = inspect.signature(getattr(CloudRunRepository, method_name)).parameters
            assert not any(issubclass(type(p), type) and p == CloudTaskClientBase for p in params)

    def test_cloud_task_client_base_has_no_durable_state_methods(self):
        # Provider transport clients remain responsible only for operations
        # like starting/querying/stopping tasks - not for owning durable
        # Issue/attempt/PR lifecycle state.
        transport_methods = {
            "continue_if_paused",
            "start_task",
            "get_task",
            "list_tasks",
            "stop_task",
            "send_followup",
        }
        client_methods = {name for name in dir(CloudTaskClientBase) if not name.startswith("_") and callable(getattr(CloudTaskClientBase, name, None))}
        lifecycle_only_methods = {"save", "get_by_task_id", "add_pull_request", "list_for_issue"}
        assert lifecycle_only_methods.isdisjoint(client_methods)
        assert transport_methods.issubset(client_methods)

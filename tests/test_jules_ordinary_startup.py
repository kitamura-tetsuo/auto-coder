"""Regression coverage for Issue #2264: Jules must be able to participate in the
unified ordinary backend pool as a task-only remote candidate, without ordinary
selection depending on the initialization of an unused synchronous manager.

These tests exercise the real configuration loader (via ``AUTO_CODER_CONFIG_PATH``
and a ``HOME`` pointed at a temporary directory) and the real
``build_backend_manager_from_config`` / ``build_backend_manager`` / ordinary
dispatcher boundary rather than mocking them, so the fix is proven at the
production-reachable boundary described by the issue rather than only at a
newly changed helper.
"""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from auto_coder.automation_config import AutomationConfig
from auto_coder.cli_commands_main import process_issues
from auto_coder.llm_backend_config import TASK_ONLY_BACKEND_TYPES, LLMBackendConfiguration, get_llm_config


def _write_config(tmp_path, toml_text: str, monkeypatch) -> None:
    config_path = tmp_path / "llm_config.toml"
    config_path.write_text(toml_text, encoding="utf-8")
    monkeypatch.setenv("AUTO_CODER_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("HOME", str(tmp_path))


def _synchronous_backends(config: LLMBackendConfiguration) -> list:
    """Mirror the synchronous-manager projection computed in ``process_issues``."""
    selected_backends = [name for group in config.get_ordinary_priority_groups() for name in group]
    return [name for name in selected_backends if config.resolve_backend_type(name) not in TASK_ONLY_BACKEND_TYPES]


# ---------------------------------------------------------------------------
# Configuration-layer coverage: the synchronous-manager projection of the
# ordinary pool, as computed by process_issues() in cli_commands_main.py.
# ---------------------------------------------------------------------------


def test_ordinary_synchronous_backends_excludes_task_only_types():
    config = LLMBackendConfiguration.load_from_dict(
        {
            "backend": {"order": ["jules", "local-b", "remote-a"]},
            "backends": {
                "jules": {"backend_type": "jules"},
                "local-b": {"backend_type": "codex"},
                "remote-a": {"backend_type": "codex-cloud"},
            },
        }
    )
    assert config.get_ordinary_priority_groups() == [["jules"], ["local-b"], ["remote-a"]]
    assert _synchronous_backends(config) == ["local-b"]


def test_ordinary_synchronous_backends_empty_for_all_remote_pool():
    config = LLMBackendConfiguration.load_from_dict(
        {
            "backend": {"order": ["jules"]},
            "backends": {"jules": {"backend_type": "jules", "enabled": True}},
        }
    )
    assert _synchronous_backends(config) == []


def test_ordinary_synchronous_backends_ignores_unlisted_enabled_backends():
    """An enabled local backend that is not part of the ordinary selector must
    never leak into the synchronous projection just because the ordinary pool
    is remote-only (this is the ``cli_backends=[]`` all-configured-backends trap
    named in the issue's implementation notes, one layer down)."""
    config = LLMBackendConfiguration.load_from_dict(
        {
            "backend": {"order": ["jules"]},
            "backends": {
                "jules": {"backend_type": "jules", "enabled": True},
                "local-unlisted": {"backend_type": "codex", "enabled": True},
            },
        }
    )
    assert _synchronous_backends(config) == []


def test_ordinary_synchronous_backends_preserves_priority_group_order():
    config = LLMBackendConfiguration.load_from_dict(
        {
            "backend": {"priority_groups": [["remote-a"], ["local-b", "local-c"]]},
            "backends": {
                "remote-a": {"backend_type": "claude-routine"},
                "local-b": {"backend_type": "codex"},
                "local-c": {"backend_type": "qwen"},
            },
        }
    )
    assert _synchronous_backends(config) == ["local-b", "local-c"]


# ---------------------------------------------------------------------------
# CLI bootstrap coverage: process-issues --only over a real configuration file
# ---------------------------------------------------------------------------


def _invoke_only(repo_name: str, issue_number: int, extra_args=()):
    return CliRunner().invoke(
        process_issues,
        [
            "--repo",
            repo_name,
            "--github-token",
            "token",
            "--only",
            f"https://github.com/{repo_name}/issues/{issue_number}",
            *extra_args,
        ],
        catch_exceptions=False,
    )


def test_process_issues_only_bootstraps_jules_only_pool_without_crashing(tmp_path, monkeypatch):
    """This reproduces the reported regression: a real ``[backend] order =
    ["jules"]`` configuration must not raise "Backend type 'jules' ... is not
    supported" during startup, and must reach the engine so ordinary dispatch
    can run."""
    _write_config(
        tmp_path,
        '[backend]\norder = ["jules"]\n\n[backends.jules]\nbackend_type = "jules"\nenabled = true\napi_key = "test-jules-key"\n',
        monkeypatch,
    )

    engine = MagicMock()
    engine.process_single.return_value = {
        "repository": "owner/repo",
        "issues_processed": [],
        "prs_processed": [],
        "errors": [],
    }

    llm_instance_spy = MagicMock()
    noedit_instance_spy = MagicMock()

    with (
        patch("auto_coder.cli_commands_main.get_repo_or_detect", return_value="owner/repo"),
        patch("auto_coder.cli_commands_main.GitHubClient.get_instance", return_value=MagicMock()),
        patch("auto_coder.cli_commands_main.AutomationEngine", return_value=engine),
        patch("auto_coder.cli_commands_main.get_current_branch", return_value="main"),
        patch("auto_coder.backend_manager.LLMBackendManager.get_llm_instance", llm_instance_spy),
        patch("auto_coder.backend_manager.LLMBackendManager.get_noedit_instance", noedit_instance_spy),
    ):
        result = _invoke_only("owner/repo", 1591)

    assert result.exit_code == 0, result.output
    assert "is not supported" not in result.output

    # Ordinary dispatch was reached: the CLI made it all the way to the engine.
    engine.process_single.assert_called_once()
    assert engine.process_single.call_args.args[:2] == ("owner/repo", "issue")

    # A remote-only ordinary pool has no synchronous candidate, so neither the
    # general nor the no-edit/message manager singleton is bootstrapped merely
    # to reach dispatch (REQ-003/REQ-005).
    llm_instance_spy.assert_not_called()
    noedit_instance_spy.assert_not_called()


def test_process_issues_only_bootstraps_mixed_pool_using_only_local_synchronous_manager(tmp_path, monkeypatch):
    """A pool with Jules first and a local backend second must not crash, and
    the general synchronous manager must be built only from the local
    candidate (REQ-002/REQ-006): the ordinary policy itself is unchanged."""
    _write_config(
        tmp_path,
        ('[backend]\norder = ["jules", "codex"]\n\n' '[backends.jules]\nbackend_type = "jules"\nenabled = true\napi_key = "test-jules-key"\n\n' '[backends.codex]\nbackend_type = "codex"\nenabled = true\n'),
        monkeypatch,
    )

    engine = MagicMock()
    engine.process_single.return_value = {
        "repository": "owner/repo",
        "issues_processed": [],
        "prs_processed": [],
        "errors": [],
    }

    captured_backend_manager_kwargs = {}
    from auto_coder import cli_helpers

    real_build_backend_manager_from_config = cli_helpers.build_backend_manager_from_config

    def spy_build_backend_manager_from_config(**kwargs):
        captured_backend_manager_kwargs.update(kwargs)
        return real_build_backend_manager_from_config(**kwargs)

    with (
        patch("auto_coder.cli_commands_main.get_repo_or_detect", return_value="owner/repo"),
        patch("auto_coder.cli_commands_main.GitHubClient.get_instance", return_value=MagicMock()),
        patch("auto_coder.cli_commands_main.AutomationEngine", return_value=engine),
        patch("auto_coder.cli_commands_main.get_current_branch", return_value="main"),
        patch("auto_coder.cli_commands_main.build_backend_manager_from_config", spy_build_backend_manager_from_config),
    ):
        result = _invoke_only("owner/repo", 1591)

    assert result.exit_code == 0, result.output
    # The synchronous manager is scoped to the local-only projection, not the
    # full ordinary pool (which still starts with the remote "jules" entry).
    assert captured_backend_manager_kwargs.get("cli_backends") == ["codex"]
    engine.process_single.assert_called_once()


def test_process_issues_only_explicit_empty_pool_still_fails_closed(tmp_path, monkeypatch):
    """An explicitly empty/disabled-only ordinary pool must keep producing its
    existing explicit diagnostic; this must not be resurrected into a
    default backend just because the synchronous-projection change touched
    the same startup block (REQ-002)."""
    _write_config(
        tmp_path,
        '[backend]\norder = ["codex"]\n\n[backends.codex]\nenabled = false\n',
        monkeypatch,
    )

    with patch("auto_coder.cli_commands_main.get_repo_or_detect", return_value="owner/repo"):
        result = _invoke_only("owner/repo", 1591)

    assert result.exit_code != 0
    assert "ordinary backend candidate pool is empty" in result.output


# ---------------------------------------------------------------------------
# Ordinary dispatcher coverage: _dispatch_issue_candidates over real config
# ---------------------------------------------------------------------------


def test_dispatch_candidates_routes_jules_only_pool_to_real_session_start(tmp_path, monkeypatch):
    from auto_coder.issue_dispatch import DispatchOutcome
    from auto_coder.issue_processor import _dispatch_issue_candidates
    from auto_coder.jules_client import JulesClient

    _write_config(
        tmp_path,
        '[backend]\norder = ["jules"]\n\n[backends.jules]\nbackend_type = "jules"\nenabled = true\napi_key = "test-jules-key"\n',
        monkeypatch,
    )
    monkeypatch.setattr("auto_coder.issue_processor.get_current_attempt", lambda *_a: 0)

    captured = {}

    def fake_start_session(self, prompt, repo, base_branch, title=None):
        captured["repo"] = repo
        captured["base_branch"] = base_branch
        captured["backend_name"] = self.backend_name
        return "session-xyz"

    monkeypatch.setattr(JulesClient, "start_session", fake_start_session)

    github = MagicMock()
    issue_data = {
        "number": 4242,
        "title": "Example issue",
        "body": "Body text",
        "labels": [],
        "state": "open",
        "user": {"login": "someone"},
    }

    assert get_llm_config(repo_name="owner/repo").get_ordinary_priority_groups() == [["jules"]]

    execution = _dispatch_issue_candidates(
        "owner/repo",
        issue_data,
        AutomationConfig(repo_name="owner/repo"),
        github,
        ["jules"],
    )

    assert execution.result.outcome is DispatchOutcome.REMOTE_ACCEPTED
    assert execution.result.backend_name == "jules"
    assert execution.result.provider == "jules"
    assert execution.result.provider_reference == "session-xyz"
    assert captured["repo"] == "owner/repo"
    assert captured["base_branch"] == "main"
    assert captured["backend_name"] == "jules"
    github.add_comment_to_issue.assert_called_once()


def test_dispatch_candidates_routes_jules_alias_using_its_own_credentials(tmp_path, monkeypatch):
    """REQ-001: an alias resolving to ``backend_type = "jules"`` (not the
    built-in ``jules`` name) is classified by its resolved type and dispatched
    using its own alias's configuration/credentials."""
    from auto_coder.issue_dispatch import DispatchOutcome
    from auto_coder.issue_processor import _dispatch_issue_candidates
    from auto_coder.jules_client import JulesClient

    _write_config(
        tmp_path,
        ('[backend]\norder = ["remote-google"]\n\n' '[backends.remote-google]\nbackend_type = "jules"\nenabled = true\napi_key = "remote-google-key"\n'),
        monkeypatch,
    )
    monkeypatch.setattr("auto_coder.issue_processor.get_current_attempt", lambda *_a: 0)

    captured = {}

    def fake_start_session(self, prompt, repo, base_branch, title=None):
        captured["backend_name"] = self.backend_name
        captured["api_key"] = self.api_key
        return "session-alias"

    monkeypatch.setattr(JulesClient, "start_session", fake_start_session)

    github = MagicMock()
    issue_data = {"number": 99, "title": "T", "body": "B", "labels": [], "state": "open", "user": {"login": "x"}}

    execution = _dispatch_issue_candidates(
        "owner/repo",
        issue_data,
        AutomationConfig(repo_name="owner/repo"),
        github,
        ["remote-google"],
    )

    assert execution.result.outcome is DispatchOutcome.REMOTE_ACCEPTED
    assert execution.result.backend_name == "remote-google"
    assert execution.result.provider == "jules"
    assert captured["backend_name"] == "remote-google"
    assert captured["api_key"] == "remote-google-key"


def test_dispatch_candidates_local_first_pool_never_touches_jules(tmp_path, monkeypatch):
    from auto_coder.issue_dispatch import DispatchOutcome
    from auto_coder.issue_processor import _dispatch_issue_candidates
    from auto_coder.jules_client import JulesClient

    _write_config(
        tmp_path,
        ('[backend]\norder = ["codex", "jules"]\n\n' '[backends.codex]\nbackend_type = "codex"\nenabled = true\n\n' '[backends.jules]\nbackend_type = "jules"\nenabled = true\napi_key = "test-jules-key"\n'),
        monkeypatch,
    )
    monkeypatch.setattr("auto_coder.issue_processor.get_current_attempt", lambda *_a: 0)
    monkeypatch.setattr("auto_coder.issue_processor._take_issue_actions", lambda *_args, **_kwargs: ["done locally"])

    jules_started = MagicMock()
    monkeypatch.setattr(JulesClient, "start_session", jules_started)

    github = MagicMock()
    issue_data = {"number": 77, "title": "T", "body": "B", "labels": [], "state": "open", "user": {"login": "x"}}

    execution = _dispatch_issue_candidates(
        "owner/repo",
        issue_data,
        AutomationConfig(repo_name="owner/repo"),
        github,
        ["codex", "jules"],
    )

    assert execution.result.outcome is DispatchOutcome.LOCAL_COMPLETED
    assert execution.result.backend_name == "codex"
    jules_started.assert_not_called()

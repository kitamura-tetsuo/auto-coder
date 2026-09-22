from datetime import datetime, timezone

import pytest

from auto_coder.llm_backend_config import LLMBackendConfiguration
from auto_coder.quota_selector import BackendQuotaEvaluation, rank_high_score_backends_by_quota


def _mixed_config(selector: dict) -> LLMBackendConfiguration:
    return LLMBackendConfiguration.load_from_dict(
        {
            "backend": selector,
            "backends": {
                "remote-a": {"backend_type": "codex-cloud"},
                "local-b": {"backend_type": "codex"},
                "remote-c": {"backend_type": "claude-routine"},
            },
        }
    )


def test_mixed_priority_groups_round_trip_and_preserve_explicit_empty_pool(tmp_path):
    config = _mixed_config(
        {
            "priority_groups": [["remote-a", "local-b"], ["remote-c"]],
            "default": "local-b",
        }
    )
    assert config.get_ordinary_priority_groups() == [["remote-a", "local-b"], ["remote-c"]]

    path = tmp_path / "llm.toml"
    config.save_to_file(str(path))
    restored = LLMBackendConfiguration.load_from_file(str(path))
    assert restored.get_ordinary_priority_groups() == [["remote-a", "local-b"], ["remote-c"]]
    assert "backend_cloud" not in path.read_text(encoding="utf-8")

    empty = _mixed_config({"priority_groups": [], "default": "local-b"})
    assert empty.get_ordinary_priority_groups() == []


@pytest.mark.parametrize(
    "backend",
    [
        {"order": "codex"},
        {"priority_groups": ["codex"]},
        {"priority_groups": [[]]},
        {"priority_groups": [[""]]},
        {"order": ["missing"]},
        {"order": [], "priority_groups": []},
    ],
)
def test_invalid_ordinary_selector_is_rejected(backend):
    with pytest.raises(ValueError):
        LLMBackendConfiguration.load_from_dict({"backend": backend})


@pytest.mark.parametrize("retired_value", [{}, {"enabled": False}, "malformed"])
def test_backend_cloud_is_rejected_even_when_empty(retired_value):
    with pytest.raises(ValueError, match=r"backend_cloud.*\[backend\]"):
        LLMBackendConfiguration.load_from_dict({"backend": {"order": ["codex"]}, "backend_cloud": retired_value})


def test_base_backend_cloud_reports_its_source_before_override_merge(tmp_path, monkeypatch):
    base = tmp_path / "base.toml"
    base.write_text("[backend_cloud]\n", encoding="utf-8")
    override = tmp_path / "owner" / "repo" / "llm_config.toml"
    override.parent.mkdir(parents=True)
    override.write_text('[backend]\norder = ["codex"]\n', encoding="utf-8")
    monkeypatch.setattr(
        "auto_coder.llm_backend_config.resolve_repo_override_path",
        lambda _repo: str(override),
    )

    with pytest.raises(ValueError, match=str(base)):
        LLMBackendConfiguration.load_from_file(str(base), repo_name="owner/repo")


def test_quota_ranking_keeps_groups_and_first_duplicate(monkeypatch):
    observations = {
        "a": BackendQuotaEvaluation(backend_name="a", quota_surplus=-0.4, actual_remaining_ratio=0.1),
        "b": BackendQuotaEvaluation(backend_name="b", quota_surplus=0.2, actual_remaining_ratio=0.5),
        "c": BackendQuotaEvaluation(backend_name="c", quota_surplus=10.0, actual_remaining_ratio=1.0),
    }
    monkeypatch.setattr(
        "auto_coder.quota_selector.evaluate_backend_quota",
        lambda backend_name, **_kwargs: observations[backend_name],
    )

    ranked = rank_high_score_backends_by_quota(
        [["a", "b"], ["c", "a"]],
        _mixed_config({"priority_groups": [["remote-a"]]}),
        now=datetime.now(timezone.utc),
    )
    assert ranked == ["b", "a", "c"]


@pytest.mark.parametrize(
    "backends",
    [
        {"custom": {"enabled": True}},
        {"custom": {"backend_type": "invented"}, "invented": {"enabled": True}},
    ],
)
def test_selected_alias_must_resolve_to_supported_implementation(backends):
    with pytest.raises(ValueError, match="resolvable implementation type"):
        LLMBackendConfiguration.load_from_dict({"backend": {"order": ["custom", "codex"]}, "backends": backends})


def test_public_json_export_preserves_priority_groups(tmp_path):
    import json

    from click.testing import CliRunner

    from auto_coder.cli_commands_config import config_to_dict, import_config

    config = LLMBackendConfiguration.load_from_dict({"backend": {"priority_groups": [["codex", "claude"], ["qwen"]]}})
    exported = config_to_dict(config)

    assert exported["backend"] == {
        "default": "codex",
        "priority_groups": [["codex", "claude"], ["qwen"]],
    }
    restored = LLMBackendConfiguration.load_from_dict(exported)
    assert restored.get_ordinary_priority_groups() == [["codex", "claude"], ["qwen"]]

    source = tmp_path / "export.json"
    target = tmp_path / "imported.toml"
    source.write_text(json.dumps(exported), encoding="utf-8")
    result = CliRunner().invoke(import_config, ["--file", str(target), str(source)])
    assert result.exit_code == 0, result.output
    imported = LLMBackendConfiguration.load_from_file(str(target))
    assert imported.get_ordinary_priority_groups() == [["codex", "claude"], ["qwen"]]


def test_inherited_noedit_filters_task_only_backends():
    config = _mixed_config({"order": ["remote-a", "local-b"]})
    assert config.get_active_noedit_backends() == ["local-b"]
    assert config.get_noedit_default_backend() == "local-b"

    cloud_only = _mixed_config({"order": ["remote-a"]})
    assert cloud_only.get_active_noedit_backends() == []
    with pytest.raises(ValueError, match="No synchronous backend"):
        cloud_only.get_noedit_default_backend()


def test_message_manager_never_constructs_inherited_cloud_candidate(monkeypatch):
    from unittest.mock import MagicMock

    from auto_coder.cli_helpers import build_message_backend_manager

    config = _mixed_config({"order": ["remote-a", "local-b"]})
    local_client = MagicMock()
    temporary = MagicMock(
        _clients={"local-b": local_client},
        _factories={"local-b": MagicMock()},
    )
    built = MagicMock(return_value=temporary)
    final_manager = MagicMock()
    monkeypatch.setattr("auto_coder.cli_helpers.get_llm_config", lambda: config)
    monkeypatch.setattr("auto_coder.cli_helpers.build_backend_manager", built)
    noedit_instance = MagicMock(side_effect=[final_manager, final_manager])
    monkeypatch.setattr(
        "auto_coder.backend_manager.LLMBackendManager.get_noedit_instance",
        noedit_instance,
    )

    assert build_message_backend_manager() is final_manager
    assert built.call_args.kwargs["selected_backends"] == ["local-b"]
    assert built.call_args.kwargs["primary_backend"] == "local-b"


def test_ordinary_dispatch_runs_opencode_before_later_codex(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    from auto_coder.automation_config import AutomationConfig
    from auto_coder.issue_dispatch import DispatchOutcome
    from auto_coder.issue_processor import _dispatch_issue_candidates

    monkeypatch.setenv("HOME", str(tmp_path))
    config = LLMBackendConfiguration.load_from_dict(
        {
            "backend": {"order": ["local-open", "codex"]},
            "backends": {"local-open": {"backend_type": "opencode"}},
        }
    )
    built = []
    monkeypatch.setattr("auto_coder.llm_backend_config.get_llm_config", lambda **_kwargs: config)
    monkeypatch.setattr(
        "auto_coder.cli_helpers.build_backend_manager",
        lambda **kwargs: built.append(kwargs["selected_backends"]) or MagicMock(),
    )
    monkeypatch.setattr("auto_coder.issue_processor._take_issue_actions", lambda *_args, **_kwargs: ["done"])
    monkeypatch.setattr("auto_coder.issue_processor.get_current_attempt", lambda *_args: 0)

    execution = _dispatch_issue_candidates(
        "owner/repo",
        {"number": 2079},
        AutomationConfig(),
        MagicMock(),
        ["local-open", "codex"],
    )

    assert execution.result.outcome is DispatchOutcome.LOCAL_COMPLETED
    assert execution.result.backend_name == "local-open"
    assert built == [["local-open"]]

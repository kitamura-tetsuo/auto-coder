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

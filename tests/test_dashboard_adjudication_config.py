"""Tests for `[dashboard_adjudication]` configuration resolution (Issue #2022, REQ-001)."""

from pathlib import Path

from src.auto_coder.llm_backend_config import get_dashboard_adjudication_config


def _write_config(tmp_path: Path, body: str) -> str:
    path = tmp_path / "config.toml"
    path.write_text(body)
    return str(path)


def _write(tmp_path: Path, name: str, content: str) -> str:
    path = tmp_path / name
    path.write_text(content)
    return str(path)


def test_disabled_by_default(tmp_path):
    config_path = _write_config(tmp_path, "")
    result = get_dashboard_adjudication_config(config_path=config_path)
    assert result.enabled is False


def test_enabled_requires_all_three_settings(tmp_path):
    secret = _write(tmp_path, "secret", "s" * 32)
    config_path = _write_config(tmp_path, f'[dashboard_adjudication]\nenabled = true\noperator_secret_file = "{secret}"\n')
    result = get_dashboard_adjudication_config(config_path=config_path)
    assert result.enabled is False
    assert result.diagnostic


def test_enabled_requires_secret_at_least_32_bytes(tmp_path):
    secret = _write(tmp_path, "secret", "short")
    token = _write(tmp_path, "token", "gh-token")
    config_path = _write_config(tmp_path, f'[dashboard_adjudication]\nenabled = true\noperator_secret_file = "{secret}"\ngithub_token_file = "{token}"\nallowed_origin = "https://dashboard.example.test"\n')
    result = get_dashboard_adjudication_config(config_path=config_path)
    assert result.enabled is False
    assert "32 bytes" in result.diagnostic


def test_enabled_requires_readable_files(tmp_path):
    token = _write(tmp_path, "token", "gh-token")
    config_path = _write_config(tmp_path, f'[dashboard_adjudication]\nenabled = true\noperator_secret_file = "{tmp_path / "missing-secret"}"\ngithub_token_file = "{token}"\nallowed_origin = "https://dashboard.example.test"\n')
    result = get_dashboard_adjudication_config(config_path=config_path)
    assert result.enabled is False
    assert "not readable" in result.diagnostic


def test_allowed_origin_must_be_exact_https_or_loopback_http(tmp_path):
    secret = _write(tmp_path, "secret", "s" * 32)
    token = _write(tmp_path, "token", "gh-token")

    for bad_origin in ("http://dashboard.example.test", "https://dashboard.example.test/path", "ftp://dashboard.example.test", ""):
        config_path = _write_config(tmp_path, f'[dashboard_adjudication]\nenabled = true\noperator_secret_file = "{secret}"\ngithub_token_file = "{token}"\nallowed_origin = "{bad_origin}"\n')
        result = get_dashboard_adjudication_config(config_path=config_path)
        assert result.enabled is False, bad_origin

    for good_origin in ("https://dashboard.example.test", "http://127.0.0.1", "http://localhost"):
        config_path = _write_config(tmp_path, f'[dashboard_adjudication]\nenabled = true\noperator_secret_file = "{secret}"\ngithub_token_file = "{token}"\nallowed_origin = "{good_origin}"\n')
        result = get_dashboard_adjudication_config(config_path=config_path)
        assert result.enabled is True, good_origin
        assert result.operator_secret_file == secret
        assert result.github_token_file == token


def test_never_falls_back_to_another_credential_on_defect(tmp_path):
    """A configuration defect disables authoring; it never widens to any other credential."""
    config_path = _write_config(tmp_path, '[dashboard_adjudication]\nenabled = true\nallowed_origin = "https://dashboard.example.test"\n')
    result = get_dashboard_adjudication_config(config_path=config_path)
    assert result.enabled is False
    assert result.operator_secret_file == ""
    assert result.github_token_file == ""

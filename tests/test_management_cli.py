from pathlib import Path
from unittest.mock import Mock

from click.testing import CliRunner

from auto_coder.cli import main


def test_management_command_bypasses_worker_and_updater(monkeypatch, tmp_path: Path) -> None:
    config = tmp_path / "management.yml"
    config.write_text(
        f"""state_root: {tmp_path / 'state'}
public_origin: http://127.0.0.1:8081
firebase_project_id: project
local_development: true
clients:
  - client_id: desktop
    redirect_uris: [http://127.0.0.1:9191/callback]
""",
        encoding="utf-8",
    )
    updater = Mock(side_effect=AssertionError("updater must not run"))
    github = Mock(side_effect=AssertionError("GitHub must not be checked"))
    serve = Mock()
    monkeypatch.setattr("auto_coder.cli.check_for_updates_and_restart", updater)
    monkeypatch.setattr("auto_coder.auth_utils.verify_github_access", github)
    monkeypatch.setattr("uvicorn.run", serve)

    result = CliRunner().invoke(main, ["management", "serve", "--config", str(config)])

    assert result.exit_code == 0, result.output
    updater.assert_not_called()
    github.assert_not_called()
    serve.assert_called_once()
    assert serve.call_args.kwargs["host"] == "127.0.0.1"
    assert serve.call_args.kwargs["port"] == 8081


def test_management_invalid_config_never_serves(monkeypatch, tmp_path: Path) -> None:
    config = tmp_path / "management.yml"
    config.write_text("public_origin: http://example.test\n", encoding="utf-8")
    serve = Mock()
    monkeypatch.setattr("uvicorn.run", serve)

    result = CliRunner().invoke(main, ["management", "serve", "--config", str(config)])

    assert result.exit_code != 0
    assert "invalid management configuration" in result.output
    serve.assert_not_called()

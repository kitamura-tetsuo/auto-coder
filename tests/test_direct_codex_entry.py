"""Unit coverage of the direct entry; fake supervisor is not live audit proof."""

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from src.auto_coder.direct_codex import DirectCodexError, DirectCodexRequest, codex_arguments, private_environment, run_direct_codex, selected_credentials
from src.auto_coder.invocation_process_supervisor import InvocationOutcome, SupervisedInvocationResult, WriterState
from src.auto_coder.local_execution_boundary import EvidenceStatus

pytestmark = pytest.mark.usefixtures("_use_real_commands")


def request(tmp_path: Path, **kwargs):
    return DirectCodexRequest(tmp_path, "Inspect the repository", "test-model", **kwargs)


@pytest.mark.parametrize("bad", [{"prompt": ""}, {"model": ""}, {"timeout_seconds": 0}, {"timeout_seconds": -1}, {"timeout_seconds": float("nan")}, {"timeout_seconds": float("inf")}])
def test_invalid_request_refused(tmp_path, bad):
    fields = dict(repository=tmp_path, prompt="prompt", model="model")
    fields.update(bad)
    with pytest.raises(DirectCodexError):
        codex_arguments(DirectCodexRequest(**fields))


def test_fixed_arguments_have_no_caller_arbitrary_flags(tmp_path):
    args = codex_arguments(request(tmp_path))
    assert args == ("--sandbox", "danger-full-access", "--ask-for-approval", "never", "-c", 'approvals_reviewer="user"', "exec", "--model", "test-model", "-")


def test_private_environment_does_not_inherit_ambient_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "/ambient-secret")
    monkeypatch.setenv("GIT_DIR", "/other-repo")
    env = private_environment(tmp_path / "home", tmp_path / "tmp", {"OPENAI_API_KEY": "synthetic"})
    assert env["CODEX_HOME"] == str(tmp_path / "home/.codex")
    assert env["OPENAI_API_KEY"] == "synthetic"
    assert "GIT_DIR" not in env
    with pytest.raises(DirectCodexError):
        private_environment(tmp_path, tmp_path, {"HOME": "/ambient-secret"})


@pytest.mark.parametrize("editable", [False, True])
@pytest.mark.parametrize("outcome", [InvocationOutcome.SUCCEEDED, InvocationOutcome.PRESTART_UNAVAILABLE, InvocationOutcome.CANCELLED, InvocationOutcome.FAILED, InvocationOutcome.TIMED_OUT])
@pytest.mark.parametrize("missing", [None, "filesystem", "observation", "direct"])
def test_entry_binds_private_root_without_caller_publication(tmp_path, editable, outcome, missing):
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    (tmp_path / "source").write_text("original")
    git("add", ".")
    git("commit", "-qm", "initial")
    observed = []

    class Supervisor:
        def run(self, launch, *, boundary):
            observed.append(launch)
            assert launch.cwd != tmp_path
            assert launch.effective_mode == ("editable" if editable else "no-edit")
            assert launch.protected_paths == (tmp_path,)
            assert launch.runtime_paths[0] != Path.home()
            # Synthetic supervisor produces evidence only for this unit test.
            if editable:
                (launch.cwd / "source").write_text("private edit")
            invocation = launch.invocation_id
            boundary.record_backend_success(invocation)
            if missing != "filesystem":
                boundary.record_filesystem_enforcement(invocation, EvidenceStatus.ESTABLISHED)
            if missing != "observation":
                boundary.record_violation_observation(invocation)
            if missing != "direct":
                boundary.record_direct_process_completion(invocation)
            return SupervisedInvocationResult(invocation, outcome, WriterState.DIRECT_PROCESS_STOPPED, 0, "done", "", direct_process_complete=True)

    try:
        with patch("src.auto_coder.direct_codex.check_runtime"), patch("src.auto_coder.direct_codex.InvocationProcessSupervisor", Supervisor), patch("src.auto_coder.direct_codex.verify_worker_repository") as readiness:
            if outcome is InvocationOutcome.SUCCEEDED and missing is None:
                result = run_direct_codex(request(tmp_path, editable=editable), executable=Path("/bin/true"), provider_environment={})
                assert result.evidence.writer_completion is EvidenceStatus.UNKNOWN
                assert result.evidence.direct_process_completion is EvidenceStatus.ESTABLISHED
                assert result.source_head == subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            else:
                with pytest.raises(DirectCodexError, match="supervised invocation failed"):
                    run_direct_codex(request(tmp_path, editable=editable), executable=Path("/bin/true"), provider_environment={})
        assert len(observed) == 1  # No alternate provider or unconfined retry.
        readiness.assert_called_once()
        assert (tmp_path / "source").read_text() == "original"
        assert (observed[0].cwd / "source").read_text() == ("private edit" if editable else "original")
    finally:
        for launch in observed:
            shutil.rmtree(launch.cwd.parent)
            shutil.rmtree(launch.runtime_paths[0].parent)


def test_root_refused_before_provider(tmp_path):
    with patch("src.auto_coder.direct_codex.os.geteuid", return_value=0), patch("src.auto_coder.direct_codex.InvocationProcessSupervisor") as supervisor:
        with pytest.raises(DirectCodexError, match="non-root"):
            run_direct_codex(request(tmp_path), executable=Path("/bin/true"), provider_environment={})
        supervisor.assert_not_called()


def test_credentials_require_explicit_selection():
    class NoRead(dict):
        def __getitem__(self, key):
            raise AssertionError("unexpected credential read")

    assert selected_credentials([], NoRead()) == {}
    assert selected_credentials(["OPENAI_API_KEY"], {"OPENAI_API_KEY": "synthetic", "CODEX_API_KEY": "other"}) == {"OPENAI_API_KEY": "synthetic"}
    with pytest.raises(DirectCodexError):
        selected_credentials(["HOME"], NoRead())

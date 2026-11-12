"""Fail-closed disposable Compose fixture configuration tests."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from scripts import dot_disposable_fixture as fixture

from tests.fixtures import supervisor_offline_process_issues as offline_worker


def source_tree(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    (root / "tests" / "fixtures").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    (root / "tests" / "fixtures" / "supervisor_offline_pid1.py").write_text("# Fixture sentinel\n", encoding="utf-8")
    return root


def test_provision_is_dockerless_private_and_observe_only(tmp_path):
    source = source_tree(tmp_path)
    root = fixture.provision(source=source, fixture_id="pr2835-test-abc", firebase_project="fixture-test-123", owner_uid=1000, owner_gid=1000)
    marker = json.loads((root / "fixture.json").read_text(encoding="utf-8"))
    compose = yaml.safe_load((root / "compose.yaml").read_text(encoding="utf-8"))
    config = yaml.safe_load((root / "management/config.yaml").read_text(encoding="utf-8"))
    policy = yaml.safe_load((root / "policy/operating-policy.yaml").read_text(encoding="utf-8"))
    assert marker["repository"] == "kitamura-tetsuo/auto-coder-test"
    assert marker["firebase_project_id"] == "fixture-test-123"
    assert marker["docker_authority_in_worker_or_management"] is False
    assert marker["endpoint_namespace"] == "management-container-only"
    assert policy["profile"] == "observe"
    assert set(policy["targets"]) == set(marker["target_ids"].values())
    assert config["local_development"] is True and config["bind_host"] == "127.0.0.1"
    assert compose["services"]["management"]["image"] == fixture.MANAGER_IMAGE
    assert all(compose["services"][name]["image"] == fixture.WORKER_IMAGE for name in ("beta", "release"))
    assert all(service["cap_drop"] == ["ALL"] for service in compose["services"].values())
    assert all("privileged" not in service and "network_mode" not in service for service in compose["services"].values())
    assert all(not any("docker.sock" in volume or "/var/lib/auto-coder" in volume for volume in service["volumes"]) for service in compose["services"].values())
    assert all(any(volume.endswith(":/fixtures:ro") for volume in compose["services"][name]["volumes"]) for name in ("beta", "release"))
    assert all(f"{root}/routing:/fixture/routing:rw" in compose["services"][name]["volumes"] for name in ("beta", "release"))
    assert f"{root}/routing:/fixture/routing:rw" not in compose["services"]["management"]["volumes"]
    assert all(len(service["networks"]) == 1 and "fixture" in service["networks"] for service in compose["services"].values())
    assert not any("ports" in service for service in compose["services"].values())
    assert any(volume.endswith(":/fixture/fixture.json:ro") for volume in compose["services"]["management"]["volumes"])
    assert all("GITHUB_TOKEN" not in service["environment"] for name, service in compose["services"].items() if name == "management")
    assert all(service["environment"]["GITHUB_TOKEN"].startswith("nonfunctional-") for name, service in compose["services"].items() if name in ("beta", "release"))
    for name, service in compose["services"].items():
        assert f"{root}/secrets:/fixture/secrets:ro" not in service["volumes"]
        assert all("github-test-scoped" not in volume for volume in service["volumes"])
        if name in ("beta", "release"):
            assert f"{root}/secrets/{name}-bridge:/fixture/secrets/{name}-bridge:ro" in service["volumes"]
    management_volumes = compose["services"]["management"]["volumes"]
    assert f"{root}/secrets/beta-bridge:/fixture/secrets/beta-bridge:ro" in management_volumes
    assert f"{root}/secrets/release-bridge:/fixture/secrets/release-bridge:ro" in management_volumes
    assert f"{root}/secrets/source-public-read:/fixture/secrets/source-public-read:ro" in management_volumes
    assert len(marker["fixture_nonce"]) == 32
    assert marker["source_fixture_available"] is False and marker["source_fixture_sha"] == ""
    assert (root / "secrets/beta-bridge").stat().st_mode & 0o077 == 0


def test_scoped_fixture_pat_is_private_and_test_repository_only(tmp_path, monkeypatch):
    token_path = tmp_path / "scoped-pat"
    monkeypatch.setenv("GITHUB_TOKEN", "nonfunctional-placeholder")
    assert offline_worker.activate_scoped_fixture_token(token_path, "kitamura-tetsuo/auto-coder-test") is False
    assert offline_worker.os.environ["GITHUB_TOKEN"] == "nonfunctional-placeholder"

    token_path.write_text("github_pat_synthetic-only", encoding="utf-8")
    token_path.chmod(0o600)
    with pytest.raises(ValueError, match="test repository"):
        offline_worker.activate_scoped_fixture_token(token_path, "kitamura-tetsuo/auto-coder")
    assert offline_worker.activate_scoped_fixture_token(token_path, "kitamura-tetsuo/auto-coder-test") is True
    assert offline_worker.os.environ["GITHUB_TOKEN"] == "github_pat_synthetic-only"

    token_path.chmod(0o644)
    with pytest.raises(ValueError, match="private regular file"):
        offline_worker.activate_scoped_fixture_token(token_path, "kitamura-tetsuo/auto-coder-test")

    token_path.unlink()
    token_path.symlink_to(tmp_path / "target")
    with pytest.raises(ValueError, match="private regular file"):
        offline_worker.activate_scoped_fixture_token(token_path, "kitamura-tetsuo/auto-coder-test")


@pytest.mark.parametrize(
    ("configured_token", "expected_flag"),
    [
        ("github_pat_synthetic-secret-never-on-argv", False),
        ("nonfunctional-ci-placeholder", True),
        ("nonfunctional-disposable-no-github-write", True),
    ],
)
def test_scoped_fixture_pat_never_enters_child_process_argv(monkeypatch, configured_token, expected_flag):
    # Capture the actual child exec argv, including the optional offline bypass.
    monkeypatch.setenv("AUTO_CODER_REPOSITORY", "kitamura-tetsuo/auto-coder-test")
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", "/unused-disposable-test")
    monkeypatch.setenv("GITHUB_TOKEN", configured_token)
    for name in (
        "AUTO_CODER_TEST_LAUNCH_AUDIT",
        "AUTO_CODER_FIXTURE_HOLD_BOOTSTRAP",
        "AUTO_CODER_FIXTURE_FAIL_STAGED_ONCE",
        "AUTO_CODER_FIXTURE_HOLD_CANDIDATE_RUNTIME",
        "AUTO_CODER_TEST_ABANDON_STARTUP_ONCE",
        "AUTO_CODER_TEST_HOLD_INSTALLED_TERMINAL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(offline_worker, "activate_scoped_fixture_token", lambda *args: False)
    captured = []
    monkeypatch.setattr(offline_worker.os, "execv", lambda executable, argv: captured.append((executable, argv)))

    offline_worker.main()

    assert len(captured) == 1
    executable, argv = captured[0]
    assert executable == offline_worker.sys.executable
    assert argv[:3] == [executable, "-m", "auto_coder.cli"]
    assert not any(value.startswith("github_pat_") for value in argv)
    if expected_flag:
        assert argv[-2:] == ["--github-token", "nonfunctional-ci-placeholder"]
    else:
        assert "--github-token" not in argv


@pytest.mark.parametrize("fixture_id", ["release-worker", "beta-worker", "../escape", "A-B", "sh", "bad space"])
def test_nonfixture_identity_is_rejected_before_creating_directory(tmp_path, fixture_id):
    source = source_tree(tmp_path)
    with pytest.raises(ValueError, match="fixture ID"):
        fixture.provision(source=source, fixture_id=fixture_id, firebase_project="fixture-test-123", owner_uid=1000, owner_gid=1000)
    assert not (source / ".agent-tmp").exists()


def test_cannot_reuse_existing_fixture_or_mutate_saved_policy(tmp_path):
    source = source_tree(tmp_path)
    root = fixture.provision(source=source, fixture_id="pr2835-test-abc", firebase_project="fixture-test-123", owner_uid=1000, owner_gid=1000)
    policy_before = (root / "policy/operating-policy.yaml").read_bytes()
    with pytest.raises(FileExistsError):
        fixture.provision(source=source, fixture_id="pr2835-test-abc", firebase_project="fixture-test-123", owner_uid=1000, owner_gid=1000)
    assert (root / "policy/operating-policy.yaml").read_bytes() == policy_before


def test_symlinked_fixture_scratch_directory_is_refused(tmp_path):
    source = source_tree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (source / ".agent-tmp").symlink_to(outside)
    with pytest.raises(ValueError, match="ordinary directory"):
        fixture.provision(source=source, fixture_id="pr2835-test-abc", firebase_project="fixture-test-123", owner_uid=1000, owner_gid=1000)
    assert not list(outside.iterdir())


def test_root_or_untrusted_project_rejected_before_writes(tmp_path):
    source = source_tree(tmp_path)
    with pytest.raises(ValueError, match="worker cannot run as root"):
        fixture.provision(source=source, fixture_id="pr2835-test-abc", firebase_project="fixture-test-123", owner_uid=0, owner_gid=1000)
    with pytest.raises(ValueError, match="Firebase project"):
        fixture.provision(source=source, fixture_id="pr2835-test-abc", firebase_project="../../prod", owner_uid=1000, owner_gid=1000)
    with pytest.raises(ValueError, match="Firebase project"):
        fixture.provision(source=source, fixture_id="pr2835-test-abc", firebase_project="auto-coder-production", owner_uid=1000, owner_gid=1000)
    assert not (source / ".agent-tmp").exists()


def test_clean_test_only_git_main_is_required_for_installed_source(tmp_path, _use_real_commands):
    source = source_tree(tmp_path)
    test_source = source_tree(tmp_path / "candidate")
    (test_source / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    def git(*arguments):
        return subprocess.run(["git", "-C", str(test_source), *arguments], check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "Disposable fixture")
    git("config", "user.email", "fixture@example.invalid")
    git("add", "-A")
    git("commit", "-qm", "initial test source")
    git("remote", "add", "origin", "https://github.com/kitamura-tetsuo/auto-coder-test.git")
    root = fixture.provision(
        source=source,
        fixture_id="pr2835-test-source",
        firebase_project="fixture-test-123",
        owner_uid=1000,
        owner_gid=1000,
        test_source=test_source,
    )
    manifest = json.loads((root / "fixture.json").read_text(encoding="utf-8"))
    compose = yaml.safe_load((root / "compose.yaml").read_text(encoding="utf-8"))
    assert manifest["source_fixture_sha"] == git("rev-parse", "HEAD")
    assert manifest["source_fixture_available"] is True
    for role in ("beta", "release"):
        assert f"{test_source}:/source:ro" in compose["services"][role]["volumes"]
        assert "PYTHONPATH" not in compose["services"][role]["environment"]
    (test_source / "untracked-source.py").write_text("# Untrusted source injection\n", encoding="utf-8")
    with pytest.raises(ValueError, match="untracked changes"):
        fixture._source_fixture_revision(test_source)


def test_wrong_test_source_remote_refused_before_fixture_creation(tmp_path, _use_real_commands):
    source = source_tree(tmp_path)
    test_source = source_tree(tmp_path / "candidate")
    (test_source / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(test_source), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(test_source), "remote", "add", "origin", "https://github.com/kitamura-tetsuo/auto-coder.git"], check=True)
    with pytest.raises(ValueError, match="test repository remote"):
        fixture.provision(
            source=source,
            fixture_id="pr2835-test-source",
            firebase_project="fixture-test-123",
            owner_uid=1000,
            owner_gid=1000,
            test_source=test_source,
        )
    assert not (source / ".agent-tmp").exists()

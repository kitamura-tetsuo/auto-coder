"""Fail-closed disposable Compose fixture configuration tests."""

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from scripts import dot_disposable_fixture as fixture

from auto_coder.automation_config import AutomationConfig, is_author_allowlisted
from auto_coder.deployment_channel import validate_repository_ownership
from auto_coder.llm_backend_config import LLMBackendConfiguration
from auto_coder.management.app import create_management_app
from auto_coder.management.beta_runtime_smoke import SmokeProfile
from auto_coder.management.oauth_config import load_management_settings
from auto_coder.management.worker_network_client import configured_worker_registry
from auto_coder.source_release_catalog import SourceReleasePolicy
from tests.fixtures import supervisor_offline_process_issues as offline_worker


def source_tree(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    (root / "tests" / "fixtures").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    (root / "tests" / "fixtures" / "supervisor_offline_pid1.py").write_text("# Fixture sentinel\n", encoding="utf-8")
    return root


def _git_source_tree(tmp_path: Path) -> Path:
    root = source_tree(tmp_path)
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    for arguments in (
        ("init", "-b", "main"),
        ("config", "user.name", "Disposable fixture"),
        ("config", "user.email", "fixture@example.invalid"),
        ("add", "-A"),
        ("commit", "-qm", "initial test source"),
        ("remote", "add", "origin", "https://github.com/kitamura-tetsuo/auto-coder-test.git"),
    ):
        subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True, text=True)
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
    assert policy["targets"][marker["target_ids"]["beta"]]["repositories"] == [fixture.BETA_WORK_REPOSITORY]
    assert policy["targets"][marker["target_ids"]["release"]]["repositories"] == [fixture.TEST_REPOSITORY]
    assert config["local_development"] is True and config["bind_host"] == "127.0.0.1"
    assert compose["services"]["management"]["image"] == fixture.MANAGER_IMAGE
    assert all(compose["services"][name]["image"] == fixture.WORKER_IMAGE for name in ("beta", "release"))
    assert all(service["cap_drop"] == ["ALL"] for service in compose["services"].values())
    assert all("privileged" not in service and "network_mode" not in service for service in compose["services"].values())
    assert all(not any("docker.sock" in volume or "/var/lib/auto-coder" in volume for volume in service["volumes"]) for service in compose["services"].values())
    assert all(any(volume.endswith(":/fixtures:ro") for volume in compose["services"][name]["volumes"]) for name in ("beta", "release"))
    assert all(f"{root}/{name}/routing:/fixture/routing:rw" in compose["services"][name]["volumes"] for name in ("beta", "release"))
    assert all("/routing:" not in volume for volume in compose["services"]["management"]["volumes"])
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
    assert not any("beta-work-read" in volume for volume in management_volumes)
    assert (root / "secrets/beta-work-read").read_bytes() == b""
    assert (root / "secrets/beta-work-read").stat().st_mode & 0o777 == 0o600
    assert len(marker["fixture_nonce"]) == 32
    assert marker["source_fixture_available"] is False and marker["source_fixture_sha"] == ""
    assert (root / "secrets/beta-bridge").stat().st_mode & 0o077 == 0
    assert "source_release_policy_path" not in config


def test_both_workers_have_viable_config_but_no_issue_or_pr_dispatch(tmp_path, monkeypatch):
    source = source_tree(tmp_path)
    root = fixture.provision(source=source, fixture_id="pr2835-test-idle", firebase_project="fixture-test-123", owner_uid=1000, owner_gid=1000)
    compose = yaml.safe_load((root / "compose.yaml").read_text(encoding="utf-8"))
    registry = json.loads(compose["services"]["management"]["environment"]["AUTO_CODER_WORKER_REGISTRY"])
    assert [target["repository"] for target in registry["workers"]] == [fixture.BETA_WORK_REPOSITORY, fixture.TEST_REPOSITORY]
    for role in ("beta", "release"):
        repository = fixture.BETA_WORK_REPOSITORY if role == "beta" else fixture.TEST_REPOSITORY
        worker = compose["services"][role]
        environment = worker["environment"]
        configuration = LLMBackendConfiguration.load_from_file(str(root / role / "runtime/llm_config.toml"))
        assert configuration.get_ordinary_priority_groups() == [["jules"]]
        assert configuration.get_active_noedit_backends() == []
        assert configuration.backends["jules"].api_key is None
        assert environment["AUTO_CODER_REPOSITORY"] == repository
        assert environment["AUTO_CODER_ISSUE_ALLOWLIST"] == environment["AUTO_CODER_PR_ALLOWLIST"] == "[]"
        assert all("api_key" not in key.lower() for key in environment)
        assert not any(f"{root}/{'release' if role == 'beta' else 'beta'}/" in volume for volume in worker["volumes"])
        for name in ("AUTO_CODER_CHANNEL", "AUTO_CODER_ARTIFACT_DIGEST", "AUTO_CODER_ISSUE_ALLOWLIST", "AUTO_CODER_PR_ALLOWLIST"):
            monkeypatch.setenv(name, environment[name])
        monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", str(root / role / "runtime"))
        monkeypatch.setenv("AUTO_CODER_OWNERSHIP_FILE", str(root / role / "routing/ownership.json"))
        identity = validate_repository_ownership(repository)
        assert identity is not None and identity.channel == role
        automation = AutomationConfig(repo_name=repository)
        assert automation.ISSUE_ALLOWLIST == automation.PR_ALLOWLIST == []
        for author_id in (None, 1, 12671456, 49699333):
            assert not is_author_allowlisted(author_id, automation.ISSUE_ALLOWLIST)
            assert not is_author_allowlisted(author_id, automation.PR_ALLOWLIST)


def test_full_source_release_configuration_has_fixed_policy_and_private_credentials(tmp_path, _use_real_commands):
    source = source_tree(tmp_path)
    test_source = _git_source_tree(tmp_path / "candidate")
    profile = SmokeProfile()
    root = fixture.provision(
        source=source,
        fixture_id="pr2835-test-release",
        firebase_project="fixture-test-123",
        owner_uid=1000,
        owner_gid=1000,
        test_source=test_source,
        source_release_workflow_id=12345,
        accepted_profile_revision=profile.content_revision,
    )
    manifest = json.loads((root / "fixture.json").read_text(encoding="utf-8"))
    compose = yaml.safe_load((root / "compose.yaml").read_text(encoding="utf-8"))
    settings = load_management_settings(root / "management/config.yaml")
    release_policy = SourceReleasePolicy.load(root / "policy/source-release-policy.json")
    assert settings.source_repository == release_policy.source_repository == fixture.TEST_REPOSITORY
    assert settings.source_release_target_id == manifest["target_ids"]["release"]
    assert settings.source_release_workflow_id == 12345
    assert settings.source_release_policy_path == Path("/fixture/policy/source-release-policy.json")
    assert settings.source_release_token_file == Path("/fixture/secrets/source-release-dispatch")
    assert settings.source_evidence_token_file == Path("/fixture/secrets/source-acceptance-publish")
    assert release_policy.accepted_beta_instance_ids == (manifest["target_ids"]["beta"],)
    assert release_policy.accepted_profiles[0].content_revision == profile.content_revision
    assert release_policy.release_ref == "refs/tags/auto-coder-release-source-current"
    assert release_policy.promotion_workflow_path == ".github/workflows/promote-accepted-source-release.yml"
    assert yaml.safe_load((root / "policy/operating-policy.yaml").read_text(encoding="utf-8"))["profile"] == "observe"
    for filename in ("source-public-read", "source-acceptance-publish", "source-release-dispatch"):
        secret = root / "secrets" / filename
        assert secret.read_bytes() == b""
        assert secret.stat().st_mode & 0o777 == 0o600
        assert f"{secret}:/fixture/secrets/{filename}:ro" in compose["services"]["management"]["volumes"]
    for role in ("beta", "release"):
        mounts = compose["services"][role]["volumes"]
        read_token = "beta-work-read" if role == "beta" else "source-public-read"
        other_read_token = "source-public-read" if role == "beta" else "beta-work-read"
        assert f"{root}/secrets/{read_token}:/fixture/secrets/github-test-scoped:ro" in mounts
        assert not any(f"/secrets/{other_read_token}:" in mount for mount in mounts)
        assert not any("source-acceptance-publish" in mount or "source-release-dispatch" in mount for mount in mounts)


@pytest.mark.parametrize("release_configuration", [False, True])
def test_generated_distinct_workloads_load_actual_registry_and_management_app(tmp_path, monkeypatch, _use_real_commands, release_configuration):
    source = source_tree(tmp_path)
    root = fixture.provision(
        source=source,
        fixture_id="pr2835-test-registry",
        firebase_project="fixture-test-123",
        owner_uid=1000,
        owner_gid=1000,
        test_source=_git_source_tree(tmp_path / "candidate") if release_configuration else None,
        source_release_workflow_id=12345 if release_configuration else None,
        accepted_profile_revision=SmokeProfile().content_revision if release_configuration else None,
    )
    compose = yaml.safe_load((root / "compose.yaml").read_text(encoding="utf-8"))
    registry = json.loads(compose["services"]["management"]["environment"]["AUTO_CODER_WORKER_REGISTRY"])
    # Translate only container mount paths; the actual production validators
    # consume all generated target, repository and machine credential values.
    for worker in registry["workers"]:
        worker["bridge_secret_file"] = str(root / "secrets" / f"{worker['channel']}-bridge")
    monkeypatch.setenv("AUTO_CODER_WORKER_REGISTRY", json.dumps(registry))
    configured = configured_worker_registry()
    assert {(target.channel, target.repository) for target in configured.values()} == {("beta", fixture.BETA_WORK_REPOSITORY), ("release", fixture.TEST_REPOSITORY)}
    settings = load_management_settings(root / "management/config.yaml")
    state_root = root / "management/oauth"
    settings = replace(
        settings,
        state_root=state_root,
        oauth=replace(settings.oauth, database_path=state_root / settings.oauth.database_path.name),
        operation_state_root=root / "management/operations",
        operating_policy_path=root / "policy/operating-policy.yaml",
        source_evidence_token_file=root / "secrets" / settings.source_evidence_token_file.name,
        source_release_policy_path=root / "policy/source-release-policy.json" if release_configuration else None,
        source_release_token_file=root / "secrets/source-release-dispatch" if release_configuration else None,
    )
    app = create_management_app(settings)
    runtime = app.state.management_runtime
    assert {target: worker.config for target, worker in runtime.clients.items()} == configured
    assert runtime.beta_acceptance is not None
    assert runtime.settings.source_repository == fixture.TEST_REPOSITORY
    assert runtime.clients[next(target for target, config in configured.items() if config.channel == "release")].config.repository == runtime.settings.source_repository

    # Sharing source provenance must never weaken unique workload ownership.
    registry["workers"][0]["repository"] = fixture.TEST_REPOSITORY
    monkeypatch.setenv("AUTO_CODER_WORKER_REGISTRY", json.dumps(registry))
    with pytest.raises(ValueError, match="distinct targets, instances, endpoints, repositories"):
        configured_worker_registry()


@pytest.mark.parametrize(
    ("workflow", "revision", "message"),
    [(None, "a" * 64, "workflow ID"), (0, "a" * 64, "workflow ID"), (True, "a" * 64, "workflow ID"), (1, None, "profile revision"), (1, "latest", "profile revision"), (1, "a" * 64, "source checkout")],
)
def test_incomplete_source_release_configuration_refuses_before_fixture_writes(tmp_path, workflow, revision, message):
    source = source_tree(tmp_path)
    with pytest.raises(ValueError, match=message):
        fixture.provision(
            source=source,
            fixture_id="pr2835-test-invalid",
            firebase_project="fixture-test-123",
            owner_uid=1000,
            owner_gid=1000,
            source_release_workflow_id=workflow,
            accepted_profile_revision=revision,
        )
    assert not (source / ".agent-tmp").exists()


@pytest.mark.parametrize("repository", [fixture.TEST_REPOSITORY, fixture.BETA_WORK_REPOSITORY])
def test_scoped_fixture_pat_is_private_and_test_repository_only(tmp_path, monkeypatch, repository):
    token_path = tmp_path / "scoped-pat"
    monkeypatch.setenv("GITHUB_TOKEN", "nonfunctional-placeholder")
    assert offline_worker.activate_scoped_fixture_token(token_path, repository) is False
    assert offline_worker.os.environ["GITHUB_TOKEN"] == "nonfunctional-placeholder"

    token_path.write_text("github_pat_synthetic-only", encoding="utf-8")
    token_path.chmod(0o600)
    with pytest.raises(ValueError, match="fixed test repositories"):
        offline_worker.activate_scoped_fixture_token(token_path, "kitamura-tetsuo/auto-coder")
    assert offline_worker.activate_scoped_fixture_token(token_path, repository) is True
    assert offline_worker.os.environ["GITHUB_TOKEN"] == "github_pat_synthetic-only"

    token_path.chmod(0o644)
    with pytest.raises(ValueError, match="private regular file"):
        offline_worker.activate_scoped_fixture_token(token_path, repository)

    token_path.unlink()
    token_path.symlink_to(tmp_path / "target")
    with pytest.raises(ValueError, match="private regular file"):
        offline_worker.activate_scoped_fixture_token(token_path, repository)


@pytest.mark.parametrize(
    ("configured_token", "expected_flag"),
    [
        ("github_pat_synthetic-secret-never-on-argv", False),
        ("nonfunctional-ci-placeholder", True),
        ("nonfunctional-disposable-no-github-write", True),
        ("nonfunctional-disposable-worker-token", True),
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


@pytest.mark.parametrize("repository", [fixture.TEST_REPOSITORY, fixture.BETA_WORK_REPOSITORY])
def test_idle_fixture_uses_production_no_publication_cli_switches(monkeypatch, repository):
    monkeypatch.setenv("AUTO_CODER_REPOSITORY", repository)
    monkeypatch.setenv("AUTO_CODER_RUNTIME_ROOT", "/unused-disposable-test")
    monkeypatch.setenv("GITHUB_TOKEN", "nonfunctional-disposable-no-github-write")
    monkeypatch.setenv("AUTO_CODER_ISSUE_ALLOWLIST", "[]")
    monkeypatch.setenv("AUTO_CODER_PR_ALLOWLIST", "[]")
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
    monkeypatch.setattr(offline_worker.os, "execv", lambda executable, argv: captured.append(argv))

    offline_worker.main()

    assert len(captured) == 1
    assert all(flag in captured[0] for flag in ("--no-auto-merge", "--no-auto-merge-dependabot-prs", "--disable-labels"))


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
    test_source = _git_source_tree(tmp_path / "candidate")

    def git(*arguments):
        return subprocess.run(["git", "-C", str(test_source), *arguments], check=True, capture_output=True, text=True).stdout.strip()

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

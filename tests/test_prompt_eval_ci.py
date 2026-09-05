import json
import os
import subprocess
from pathlib import Path

import pytest

RUNNER = Path(__file__).parents[1] / "prompt-evals/run_prompt_evals.py"
pytestmark = pytest.mark.usefixtures("_use_real_commands")


def commit(repo: Path, message: str) -> str:
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=repo, check=True, capture_output=True)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()


def setup_repo(tmp_path: Path, targets: list[dict[str, object]]) -> tuple[Path, str]:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "prompt-evals").mkdir()
    (tmp_path / "prompt-evals/registry.json").write_text(json.dumps({"schema_version": 1, "targets": targets}))
    (tmp_path / "src").mkdir()
    (tmp_path / "src/prompts.yaml").write_text("a: {prompt: original}\nb: {prompt: original}\nshared: original\nshared.safety: original\n")
    return tmp_path, commit(tmp_path, "base")


def invoke(repo: Path, base: str, fake_npx: Path) -> subprocess.CompletedProcess[str]:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    return subprocess.run(["python", str(RUNNER), "--repo", str(repo), "--base", base, "--head", head, "--npx", str(fake_npx)], text=True, capture_output=True)


def target(name: str, keys: list[str]) -> dict[str, object]:
    root = f"prompt-evals/targets/{name}"
    return {"id": name, "prompt_dependencies": [{"path": "src/prompts.yaml", "keys": keys}], "config": f"{root}/promptfooconfig.yaml", "cases": [f"{root}/cases/*.yaml"]}


def fake_npx(tmp_path: Path) -> Path:
    script = tmp_path / "npx"
    script.write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$NPX_LOG"\nexit "${NPX_EXIT:-0}"\n')
    script.chmod(0o755)
    return script


def test_empty_registry_and_unrelated_change_do_not_invoke_provider(tmp_path: Path) -> None:
    repo, base = setup_repo(tmp_path, [])
    (repo / "unrelated.py").write_text("value = 1\n")
    commit(repo, "unrelated")
    result = invoke(repo, base, repo / "missing-npx")
    assert result.returncode == 0
    assert "No prompt-evaluation targets affected" in result.stdout


def test_prompt_key_and_shared_dependencies_select_only_affected_targets(tmp_path: Path) -> None:
    targets = [target("a", ["a.prompt", "shared"]), target("b", ["b.prompt", "shared"]), target("c", ["b.prompt"])]
    repo, base = setup_repo(tmp_path, targets)
    for name in ("a", "b", "c"):
        directory = repo / f"prompt-evals/targets/{name}"
        (directory / "cases").mkdir(parents=True)
        (directory / "promptfooconfig.yaml").write_text("description: test\n")
        (directory / "cases/case.yaml").write_text("tests: []\n")
    commit(repo, "register fixtures")
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    (repo / "src/prompts.yaml").write_text("a: {prompt: changed}\nb: {prompt: original}\nshared: original\n")
    commit(repo, "change a")
    log = repo / "npx.log"
    os.environ["NPX_LOG"] = str(log)
    result = invoke(repo, base, fake_npx(repo))
    assert result.returncode == 0
    assert "target: a" in result.stdout
    assert "target: b" not in result.stdout
    assert "target: c" not in result.stdout
    assert log.read_text().count("promptfoo@0.118.8") == 1

    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    (repo / "src/prompts.yaml").write_text("a: {prompt: changed}\nb: {prompt: original}\nshared: changed\n")
    commit(repo, "change shared")
    log.unlink()
    result = invoke(repo, base, fake_npx(repo))
    assert result.returncode == 0
    assert "target: a" in result.stdout and "target: b" in result.stdout
    assert "target: c" not in result.stdout
    assert log.read_text().count("promptfoo@0.118.8") == 2


def test_flat_dotted_shared_dependency_selects_only_consumers(tmp_path: Path) -> None:
    targets = [target("a", ["shared.safety"]), target("b", ["shared.safety"]), target("independent", ["a.prompt"])]
    repo, base = setup_repo(tmp_path, targets)
    for name in ("a", "b", "independent"):
        directory = repo / f"prompt-evals/targets/{name}"
        (directory / "cases").mkdir(parents=True)
        (directory / "promptfooconfig.yaml").write_text("description: test\n")
        (directory / "cases/case.yaml").write_text("tests: []\n")
    commit(repo, "evaluation fixtures")
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    (repo / "src/prompts.yaml").write_text("a: {prompt: original}\nb: {prompt: original}\nshared: original\nshared.safety: changed\n")
    commit(repo, "change flat shared prompt")
    log = repo / "npx.log"
    os.environ["NPX_LOG"] = str(log)
    result = invoke(repo, base, fake_npx(repo))
    assert result.returncode == 0
    assert "target: a" in result.stdout and "target: b" in result.stdout
    assert "target: independent" not in result.stdout
    assert log.read_text().count("promptfoo@0.118.8") == 2


def test_renamed_dependency_is_not_silently_ignored(tmp_path: Path) -> None:
    repo, base = setup_repo(tmp_path, [target("a", ["a.prompt"])])
    directory = repo / "prompt-evals/targets/a"
    (directory / "cases").mkdir(parents=True)
    (directory / "promptfooconfig.yaml").write_text("description: test\n")
    (directory / "cases/case.yaml").write_text("tests: []\n")
    commit(repo, "evaluation fixtures")
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    (repo / "src/prompts.yaml").rename(repo / "src/renamed.yaml")
    commit(repo, "rename prompt dependency")
    log = repo / "npx.log"
    os.environ["NPX_LOG"] = str(log)
    result = invoke(repo, base, fake_npx(repo))
    assert result.returncode == 0
    assert "target: a" in result.stdout
    assert log.read_text().count("promptfoo@0.118.8") == 1


def test_recursive_corpus_change_selects_owning_target(tmp_path: Path) -> None:
    recursive = target("a", ["a.prompt"])
    recursive["cases"] = ["prompt-evals/targets/a/cases/**/*.yaml"]
    repo, base = setup_repo(tmp_path, [recursive, target("b", ["b.prompt"])])
    for name in ("a", "b"):
        directory = repo / f"prompt-evals/targets/{name}"
        (directory / "cases/deep/nested").mkdir(parents=True)
        (directory / "promptfooconfig.yaml").write_text("description: test\n")
        (directory / "cases/deep/nested/case.yaml").write_text("tests: []\n")
    commit(repo, "evaluation fixtures")
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    (repo / "prompt-evals/targets/a/cases/deep/nested/case.yaml").write_text("tests: [{vars: {changed: true}}]\n")
    commit(repo, "change nested case")
    log = repo / "npx.log"
    os.environ["NPX_LOG"] = str(log)
    result = invoke(repo, base, fake_npx(repo))
    assert result.returncode == 0
    assert "target: a" in result.stdout
    assert "target: b" not in result.stdout
    assert log.read_text().count("promptfoo@0.118.8") == 1


@pytest.mark.parametrize(
    ("pattern", "hidden_path"),
    [
        ("prompt-evals/targets/a/cases/*.yaml", ".notes.yaml"),
        ("prompt-evals/targets/a/cases/**/*.yaml", ".draft/note.yaml"),
    ],
)
def test_hidden_files_excluded_by_corpus_glob_do_not_select_target(tmp_path: Path, pattern: str, hidden_path: str) -> None:
    registered = target("a", ["a.prompt"])
    registered["cases"] = [pattern]
    repo, base = setup_repo(tmp_path, [registered])
    directory = repo / "prompt-evals/targets/a"
    (directory / "cases").mkdir(parents=True)
    (directory / "promptfooconfig.yaml").write_text("tests: cases/case.yaml\n")
    (directory / "cases/case.yaml").write_text("tests: []\n")
    hidden = directory / "cases" / hidden_path
    hidden.parent.mkdir(parents=True, exist_ok=True)
    hidden.write_text("draft: original\n")
    commit(repo, "evaluation fixtures")
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    hidden.write_text("draft: changed\n")
    commit(repo, "change excluded draft")
    result = invoke(repo, base, repo / "missing-npx")
    assert result.returncode == 0
    assert "No prompt-evaluation targets affected" in result.stdout
    assert "Evaluating affected target" not in result.stdout


def test_corpus_change_runs_promptfoo_and_propagates_failure(tmp_path: Path) -> None:
    repo, base = setup_repo(tmp_path, [target("a", ["a.prompt"])])
    directory = repo / "prompt-evals/targets/a"
    (directory / "cases").mkdir(parents=True)
    (directory / "promptfooconfig.yaml").write_text("description: test\n")
    commit(repo, "config")
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    (directory / "cases/new.yaml").write_text("tests: []\n")
    commit(repo, "case")
    os.environ["NPX_LOG"] = str(repo / "npx.log")
    os.environ["NPX_EXIT"] = "7"
    result = invoke(repo, base, fake_npx(repo))
    os.environ.pop("NPX_EXIT")
    assert result.returncode == 7
    assert "target: a" in result.stdout


def test_malformed_affected_dependency_fails_closed(tmp_path: Path) -> None:
    broken = target("a", ["a.prompt"])
    broken["prompt_dependencies"] = [{"path": "src/prompts.yaml", "keys": "a.prompt"}]
    repo, base = setup_repo(tmp_path, [broken])
    (repo / "src/prompts.yaml").write_text("a: {prompt: changed}\n")
    commit(repo, "prompt")
    result = invoke(repo, base, repo / "missing-npx")
    assert result.returncode == 2
    assert "failed closed" in result.stderr

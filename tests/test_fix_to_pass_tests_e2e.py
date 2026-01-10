import os
import subprocess

import pytest
from click.testing import CliRunner

from src.auto_coder.cli import fix_to_pass_tests_command

# Define the path to the test repository
TEST_REPO_PATH = "/home/node/src/auto-coder-test"


@pytest.fixture
def setup_test_repo():
    """
    Fixture to set up the test repository.
    It switches to the failing branch and resets any changes.
    """
    original_cwd = os.getcwd()
    os.chdir(TEST_REPO_PATH)

    # Ensure we are on the scenario branch and clean
    subprocess.run(["git", "checkout", "scenario/fails_math"], check=True, capture_output=True)
    subprocess.run(["git", "reset", "--hard", "HEAD"], check=True, capture_output=True)
    subprocess.run(["git", "clean", "-fd"], check=True, capture_output=True)

    yield

    # Teardown: Reset changes and switch back to main (optional but good practice)
    subprocess.run(["git", "reset", "--hard", "HEAD"], check=True, capture_output=True)
    subprocess.run(["git", "checkout", "main"], check=True, capture_output=True)
    os.chdir(original_cwd)


@pytest.mark.skip(reason="This test is for a specific containerized environment and not relevant to CI")
def test_fix_to_pass_tests_e2e(setup_test_repo, _use_real_home, _use_real_commands):
    """
    End-to-end test for fix-to-pass-tests command.
    It runs the command against the auto-coder-test repository
    and verifies that the failing test is fixed.
    """
    runner = CliRunner()

    # We are already in TEST_REPO_PATH due to the fixture

    # Fetch GitHub token from environment or gh CLI
    try:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            token = subprocess.check_output(["gh", "auth", "token"], text=True).strip()
    except Exception:
        pytest.skip("No GitHub token available for E2E test")

    # Invoke the command
    # We use --max-attempts 3 to ensure it has enough tries,
    # but based on manual test it worked in 1-3 tries.
    result = runner.invoke(fix_to_pass_tests_command, ["--max-attempts", "3", "--github-token", token], catch_exceptions=False)

    # Assert command success
    assert result.exit_code == 0, f"Command failed with output: {result.output}"
    assert "Tests passed" in result.output

    # Assert the file content was actually changed to the correct value
    # The failing test expected 4, but 1+2=3. Fix should make it expect 3.
    with open("tests/test_calc.py", "r") as f:
        content = f.read()

    assert "assert add(1, 2) == 3" in content, "The test file was not correctly fixed to expect 3"

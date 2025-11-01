import os
import subprocess
import sys
import textwrap


def _write_codex_stub(bin_dir):
    script_path = bin_dir / "codex"
    script_path.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            if [[ "$1" == "--version" ]]; then
              echo "codex stub 1.0.0"
              exit 0
            fi

            if [[ "$1" == "exec" ]]; then
              shift
              limit_message="2025-09-24 16:13:47.530 | INFO     | auto_coder/utils.py:155 in _run_with_streaming - [2025-09-24T07:13:47] ERROR: You've hit your usage limit. Upgrade to Pro (https://openai.com/chatgpt/pricing) or try again in 2 hours 22 minutes."
              >&2 echo "$limit_message"
              exit 1
            fi

            >&2 echo "codex stub: unsupported invocation $*"
            exit 1
            """
        )
    )
    script_path.chmod(0o755)
    return script_path


def test_codex_cli_usage_limit_detection_e2e(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_codex_stub(bin_dir)

    original_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", f"{bin_dir}:{original_path}")

    python_code = textwrap.dedent(
        """
        import os
        from src.auto_coder.codex_client import CodexClient
        from src.auto_coder.exceptions import AutoCoderUsageLimitError


        def main() -> int:
            client = CodexClient()
            try:
                client._run_gemini_cli("Detect usage limit condition")
            except AutoCoderUsageLimitError as exc:  # noqa: PERF203 - clarity first in e2e script
                print(f"CAUGHT_USAGE_LIMIT: {exc}")
                return 0
            except Exception as exc:  # pragma: no cover - defensive path for subprocess validation
                print(f"UNEXPECTED_EXCEPTION: {exc}", file=os.sys.stderr)
                return 2
            else:  # pragma: no cover - ensures failure if limit is not detected
                print("NO_USAGE_LIMIT_DETECTED", file=os.sys.stderr)
                return 1


        if __name__ == "__main__":
            raise SystemExit(main())
        """
    )

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{original_path}"

    # Ensure PYTHONPATH includes site-packages so subprocess can find installed modules
    import site
    user_site = site.getusersitepackages()
    system_sites = site.getsitepackages()
    python_path = os.pathsep.join([user_site] + system_sites)
    env["PYTHONPATH"] = python_path

    result = subprocess.run(
        [sys.executable, "-c", python_code],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "caught_usage_limit" in result.stdout.lower()

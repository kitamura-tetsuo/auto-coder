import os
import subprocess
import sys
import textwrap


def _build_python_snippet() -> str:
    return textwrap.dedent(
        """
        import os
        import sys
        from src.auto_coder.logger_config import setup_logger
        from src.auto_coder.utils import CommandExecutor


        def main() -> int:
            setup_logger(log_level="INFO")
            result = CommandExecutor.run_command(
                [sys.executable, "-c", "print('verbose hello')"],
                stream_output=False,
            )
            print(f"RESULT_STDOUT={result.stdout.strip()}")
            return 0


        if __name__ == "__main__":
            raise SystemExit(main())
        """
    )


def test_verbose_logging_emits_command_trace():
    python_snippet = _build_python_snippet()

    env_verbose = os.environ.copy()
    env_verbose["AUTOCODER_VERBOSE"] = "1"

    result_verbose = subprocess.run(
        [sys.executable, "-c", python_snippet],
        capture_output=True,
        text=True,
        check=True,
        env=env_verbose,
    )

    assert "Executing command" in result_verbose.stdout
    assert "RESULT_STDOUT=verbose hello" in result_verbose.stdout

    env_quiet = os.environ.copy()
    env_quiet.pop("AUTOCODER_VERBOSE", None)

    result_quiet = subprocess.run(
        [sys.executable, "-c", python_snippet],
        capture_output=True,
        text=True,
        check=True,
        env=env_quiet,
    )

    assert "Executing command" not in result_quiet.stdout
    assert "RESULT_STDOUT=verbose hello" in result_quiet.stdout

import json
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Add src to path to import log_utils and git_info
sys.path.insert(0, str(Path(__file__).parent.parent))

from auto_coder.git_info import get_current_repo_name
from auto_coder.log_utils import LogEntry, get_test_log_dir


def get_runner_prefix():
    """Determines if uv is available and returns the appropriate runner command prefix."""
    if shutil.which("uv"):
        return ["uv", "run"]
    return []


def collect_and_run():
    """
    Runs pytest with the given arguments, captures the output, and logs the results.
    """
    args = sys.argv[1:]
    repo_name = get_current_repo_name()
    if not repo_name:
        print("Error: Could not determine repository name.", file=sys.stderr)
        sys.exit(1)

    log_dir = get_test_log_dir(repo_name)
    raw_log_dir = log_dir / "raw"
    raw_log_dir.mkdir(parents=True, exist_ok=True)

    # Determine the test target for the log filename
    test_target = "all"
    if args:
        # Assuming the first argument might be a test file
        first_arg = args[0]
        if not first_arg.startswith("-") and (first_arg.endswith(".py") or Path(first_arg).is_file()):
            test_target = Path(first_arg).stem

    timestamp = datetime.now()
    log_filename = f"{timestamp.strftime('%Y%m%d_%H%M%S')}_local_{test_target}.json"

    runner = get_runner_prefix()
    command = runner + ["pytest"] + args
    command_str = " ".join(command)
    print(f"Executing command: {command_str}")

    stdout_lines = []
    stderr_lines = []
    return_code = 0

    try:
        # Pass environment variable to suppress duplicate logging from conftest.py
        env = os.environ.copy()
        env["AUTO_CODER_LOG_COLLECTOR_ACTIVE"] = "1"

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # Line buffered
            universal_newlines=True,
            env=env,
        )

        # Stream output in real-time
        # Note: This simple loop might deadlock if one pipe fills up while we read the other.
        # Ideally, we should use selectors or threads, but for simple test output,
        # we can try to read line by line or use a simpler approach.
        # However, to be robust, let's use a simpler approach:
        # Popen.communicate() waits, so we can't stream with it.
        # We will use select or threads if needed, but for now let's use a simple valid approach:
        # Just run the command and stream to stdout/stderr normally if we could,
        # BUT we need to CAPTURE it too.

        # Correct approach for streaming AND capturing:
        # We can use a thread to read pipes, or use p.stdout.readline() in a loop.
        # Since we have two pipes, threads are safer.

        import threading

        def reader(pipe, output_list, out_stream):
            for line in iter(pipe.readline, ""):
                out_stream.write(line)
                out_stream.flush()  # Ensure it appears immediately
                output_list.append(line)

        t_out = threading.Thread(target=reader, args=(process.stdout, stdout_lines, sys.stdout))
        t_err = threading.Thread(target=reader, args=(process.stderr, stderr_lines, sys.stderr))

        t_out.start()
        t_err.start()

        return_code = process.wait()

        t_out.join()
        t_err.join()

    except FileNotFoundError:
        print(f"Error: Command not found. Is pytest installed? Command: '{command_str}'", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"An unexpected error occurred: {e}", file=sys.stderr)
        sys.exit(1)

    full_stdout = "".join(stdout_lines)
    full_stderr = "".join(stderr_lines)

    # Handle raw logs
    raw_log_file_path = None
    source_raw_log_dir = Path("./logs/tests")
    if source_raw_log_dir.exists() and source_raw_log_dir.is_dir():
        for log_file in source_raw_log_dir.iterdir():
            if log_file.is_file():
                try:
                    shutil.move(str(log_file), str(raw_log_dir))
                    raw_log_file_path = str(raw_log_dir / log_file.name)
                    print(f"Moved raw log file: {log_file} to {raw_log_file_path}")
                    # For now, just handle the first found log file
                    break
                except Exception as e:
                    print(f"Error moving raw log file {log_file}: {e}", file=sys.stderr)

    log_entry = LogEntry(
        ts=timestamp.isoformat(),
        source="local",
        repo=repo_name,
        command=command_str,
        exit_code=return_code,
        stdout=full_stdout,
        stderr=full_stderr,
        file=raw_log_file_path,
        meta={
            "os": platform.system(),
            "python_version": platform.python_version(),
        },
    )

    try:
        log_entry.save(log_dir, log_filename)
        print(f"Test log saved to: {log_dir / log_filename}")
    except Exception as e:
        print(f"Error saving test log: {e}", file=sys.stderr)

    sys.exit(return_code)


if __name__ == "__main__":
    collect_and_run()

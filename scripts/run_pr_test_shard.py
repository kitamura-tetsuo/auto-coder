#!/usr/bin/env python3
"""Run one PR-test shard with bounded, timeout-only retry semantics."""

from __future__ import annotations

import argparse
import os
import selectors
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import FrameType

from loguru import logger

REPORT_PATHS = (Path("htmlcov"), Path(".coverage"), Path("coverage.xml"), Path("junit.xml"))
POLL_INTERVAL_SECONDS = 0.05
KILL_CONFIRM_SECONDS = 2.0
cancel_signal: int | None = None


def _handle_cancel(signum: int, _frame: FrameType | None) -> None:
    global cancel_signal
    cancel_signal = signum


def _group_members(pgid: int) -> list[int]:
    """Return live Linux processes in pgid; zombies are already stopped."""
    members: list[int] = []
    proc = Path("/proc")
    if proc.is_dir():
        for item in proc.iterdir():
            if not item.name.isdigit():
                continue
            try:
                fields = (item / "stat").read_text().split()
                if int(fields[4]) == pgid and fields[2] != "Z":
                    members.append(int(item.name))
            except (OSError, IndexError, ValueError):
                continue
        return members
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return []
    except PermissionError:
        return [pgid]
    return [pgid]


def _remove_reports() -> None:
    for path in REPORT_PATHS:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


def _retain_partial_reports(log_dir: Path, attempt: int) -> None:
    destination = log_dir / f"attempt-{attempt}-partial-reports"
    for path in REPORT_PATHS:
        if not path.exists():
            continue
        destination.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), destination / path.name)


def _drain(selector: selectors.BaseSelector, output_files: dict[str, object]) -> None:
    for key, _ in selector.select(timeout=0):
        stream = key.fileobj
        try:
            chunk = os.read(stream.fileno(), 65536)  # type: ignore[union-attr]
        except BlockingIOError:
            continue
        if not chunk:
            selector.unregister(stream)
            stream.close()  # type: ignore[union-attr]
            continue
        output_file = output_files[key.data]
        output_file.write(chunk)  # type: ignore[attr-defined]
        output_file.flush()  # type: ignore[attr-defined]
        target = sys.stdout.buffer if key.data == "stdout" else sys.stderr.buffer
        try:
            target.write(chunk)
            target.flush()
        except BrokenPipeError:
            # The artifact logs remain authoritative if the console consumer disappears.
            pass


def _stop_group(process: subprocess.Popen[bytes], pgid: int, grace: float) -> bool:
    members = _group_members(pgid)
    if members:
        logger.warning("Sending SIGTERM to managed process group {} (members={})", pgid, members)
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace
    while _group_members(pgid) and time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
    members = _group_members(pgid)
    if members:
        logger.warning("Escalating managed process group {} to SIGKILL (members={})", pgid, members)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    confirmation_deadline = time.monotonic() + KILL_CONFIRM_SECONDS
    while _group_members(pgid) and time.monotonic() < confirmation_deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
    try:
        process.wait(timeout=KILL_CONFIRM_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    return not _group_members(pgid)


def _run_attempt(group: int, attempt: int, timeout: float, grace: float, log_dir: Path) -> str:
    command = ["bash", "scripts/test.sh", "--splits", "4", "--group", str(group)]
    supervisor_log = log_dir / f"attempt-{attempt}.supervisor.log"
    sink = logger.add(
        supervisor_log,
        format="{time} | {level} | {file.name}:{function}:{line} | {message}",
        enqueue=False,
    )
    started = time.monotonic()
    logger.info("Attempt {} launching: {}", attempt, " ".join(command))
    try:
        # Prove all incremental output sinks are writable before launching work.
        for suffix in ("stdout", "stderr"):
            (log_dir / f"attempt-{attempt}.{suffix}.log").touch()
    except OSError as exc:
        logger.error("Attempt {} infrastructure failure preparing logs: {}", attempt, exc)
        logger.remove(sink)
        return "infrastructure"
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        logger.error("Attempt {} infrastructure failure while launching: {}", attempt, exc)
        logger.remove(sink)
        return "infrastructure"

    pgid = process.pid
    try:
        if os.getpgid(process.pid) != pgid:
            logger.error("Attempt {} could not establish an isolated process group", attempt)
            _stop_group(process, pgid, grace)
            logger.remove(sink)
            return "infrastructure"
    except ProcessLookupError:
        # A very short command can exit before validation; it still owned its new session.
        pass

    selector = selectors.DefaultSelector()
    assert process.stdout is not None and process.stderr is not None
    for stream, name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)

    deadline = started + timeout
    result = "infrastructure"
    with (log_dir / f"attempt-{attempt}.stdout.log").open("ab", buffering=0) as stdout_log, (log_dir / f"attempt-{attempt}.stderr.log").open("ab", buffering=0) as stderr_log:
        output_files = {"stdout": stdout_log, "stderr": stderr_log}
        while True:
            _drain(selector, output_files)
            now = time.monotonic()
            if cancel_signal is not None:
                logger.error("Attempt {} cancelled by signal {} after {:.3f}s", attempt, cancel_signal, now - started)
                result = "cancelled" if _stop_group(process, pgid, grace) else "cleanup-failure"
                break
            if now >= deadline:
                logger.error("Attempt {} timed out after {:.3f}s", attempt, now - started)
                result = "timeout" if _stop_group(process, pgid, grace) else "cleanup-failure"
                break
            status = process.poll()
            if status is not None and not _group_members(pgid):
                _drain(selector, output_files)
                elapsed = time.monotonic() - started
                if status == 0:
                    logger.success("Attempt {} succeeded with status 0 after {:.3f}s", attempt, elapsed)
                    result = "success"
                else:
                    logger.error("Attempt {} ordinary failure with status {} after {:.3f}s", attempt, status, elapsed)
                    result = "failure"
                break
            selector.select(timeout=min(POLL_INTERVAL_SECONDS, max(0.0, deadline - now)))

    for key in list(selector.get_map().values()):
        selector.unregister(key.fileobj)
        key.fileobj.close()
    selector.close()
    logger.remove(sink)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", type=int, choices=range(1, 5), required=True)
    parser.add_argument("--attempt-timeout", type=float, default=180)
    parser.add_argument("--termination-grace", type=float, default=10)
    parser.add_argument("--max-attempts", type=int, default=2, choices=(2,))
    parser.add_argument("--log-dir", type=Path, required=True)
    args = parser.parse_args()
    args.log_dir.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(
        sys.stderr,
        colorize=True,
        format="{time} | {level} | {file.name}:{function}:{line} | {message}",
    )
    logger.add(
        args.log_dir / "supervisor.log",
        format="{time} | {level} | {file.name}:{function}:{line} | {message}",
        enqueue=False,
    )
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _handle_cancel)

    for attempt in range(1, args.max_attempts + 1):
        _remove_reports()
        outcome = _run_attempt(args.group, attempt, args.attempt_timeout, args.termination_grace, args.log_dir)
        if outcome == "success":
            return 0
        if outcome != "timeout" or attempt == args.max_attempts or cancel_signal is not None:
            logger.error("Shard {} stopped after attempt {} ({})", args.group, attempt, outcome)
            return 1
        _retain_partial_reports(args.log_dir, attempt)
        if cancel_signal is not None:
            logger.error("Cancellation observed after cleanup; retry suppressed")
            return 1
        logger.warning("Attempt 1 cleanup completed; starting retry immediately")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

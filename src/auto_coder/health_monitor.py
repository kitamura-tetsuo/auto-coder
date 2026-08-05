"""Runtime health monitoring and crash diagnostics for long-running runs.

Auto-Coder is normally started once and left running for hours.  When such a
run stops unexpectedly the regular application log rarely contains the reason:
the process may have been killed by the OOM killer, terminated by a signal,
restarted by the auto-updater, or simply blocked forever inside a subprocess.

This module records the information needed to tell those cases apart:

* a periodic resource snapshot (RSS, peak RSS, system/cgroup memory, file
  descriptors, threads, child processes, CPU time, asyncio tasks) written both
  to the normal log and to a machine readable JSON Lines file,
* heartbeats from the producer/worker loops so a hang can be located in time,
* stack dumps of every thread when a heartbeat is overdue or a fatal signal
  arrives,
* explicit records for signals, uncaught exceptions and interpreter exit.

Environment Variables
---------------------

AUTO_CODER_HEALTH_LOG_ENABLED
    "0"/"false"/"no"/"off" disables health logging entirely (default: enabled).
AUTO_CODER_HEALTH_LOG_DIR
    Directory for ``health-YYYY-MM-DD.jsonl`` and ``diagnostics-<pid>.log``
    (default: ``~/.auto-coder/logs``).
AUTO_CODER_HEALTH_INTERVAL_SECONDS
    Seconds between snapshots (default: 60).
AUTO_CODER_HEALTH_STALL_SECONDS
    Seconds without a heartbeat before a stall is reported and all thread
    stacks are dumped (default: 3600).
AUTO_CODER_HEALTH_RETENTION_DAYS
    Days of JSONL files to keep (default: 14).
AUTO_CODER_HEALTH_DEEP
    "1" also counts every GC tracked object in each snapshot (slower).
AUTO_CODER_HEALTH_TRACEMALLOC
    "1" starts tracemalloc and logs the top allocation sites with each
    snapshot; use it to locate a memory leak.
"""

import atexit
import faulthandler
import gc
import json
import os
import platform
import resource
import shutil
import signal
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import FrameType
from typing import IO, TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from asyncio import AbstractEventLoop

from .logger_config import get_logger

logger = get_logger(__name__)

_BYTES_PER_MB = 1024.0 * 1024.0
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}

DEFAULT_INTERVAL_SECONDS = 60.0
DEFAULT_STALL_SECONDS = 3600.0
DEFAULT_RETENTION_DAYS = 14


def _env_flag(name: str, default: bool = False) -> bool:
    """Return a boolean environment flag."""

    raw = os.environ.get(name, "").strip().lower()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    return default


def _env_float(name: str, default: float) -> float:
    """Return a positive float environment value, falling back to ``default``."""

    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int) -> int:
    """Return a positive int environment value, falling back to ``default``."""

    return int(_env_float(name, float(default)))


def get_health_log_dir() -> Path:
    """Return the directory used for health and diagnostics files."""

    configured = os.environ.get("AUTO_CODER_HEALTH_LOG_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".auto-coder" / "logs"


@dataclass
class ResourceSnapshot:
    """A point-in-time view of the process and of the machine it runs on."""

    timestamp: str = ""
    reason: str = "periodic"
    pid: int = 0
    uptime_seconds: float = 0.0
    stage: str = ""
    seconds_since_heartbeat: float = 0.0
    rss_mb: float = 0.0
    peak_rss_mb: float = 0.0
    vms_mb: float = 0.0
    rss_growth_mb: float = 0.0
    system_total_mb: float = 0.0
    system_available_mb: float = 0.0
    system_available_percent: float = 0.0
    swap_used_mb: float = 0.0
    cgroup_limit_mb: float = 0.0
    cgroup_usage_mb: float = 0.0
    cgroup_usage_percent: float = 0.0
    cgroup_oom_kill_count: int = 0
    cpu_user_seconds: float = 0.0
    cpu_system_seconds: float = 0.0
    cpu_children_seconds: float = 0.0
    major_page_faults: int = 0
    thread_count: int = 0
    python_thread_count: int = 0
    open_fd_count: int = 0
    child_process_count: int = 0
    asyncio_task_count: int = 0
    gc_generation_counts: str = ""
    gc_tracked_objects: int = 0
    load_average_1m: float = 0.0
    disk_free_mb: float = 0.0

    def format_summary(self) -> str:
        """Return a single compact line for the human readable log."""

        parts = [
            f"reason={self.reason}",
            f"uptime={self.uptime_seconds / 3600.0:.2f}h",
            f"rss={self.rss_mb:.1f}MB (peak {self.peak_rss_mb:.1f}MB, {self.rss_growth_mb:+.1f}MB since start)",
            f"sys_avail={self.system_available_mb:.0f}MB ({self.system_available_percent:.1f}%)",
        ]
        if self.cgroup_limit_mb > 0:
            parts.append(f"cgroup={self.cgroup_usage_mb:.0f}/{self.cgroup_limit_mb:.0f}MB ({self.cgroup_usage_percent:.1f}%)")
        if self.cgroup_oom_kill_count:
            parts.append(f"cgroup_oom_kills={self.cgroup_oom_kill_count}")
        parts.extend(
            [
                f"threads={self.thread_count}",
                f"fds={self.open_fd_count}",
                f"children={self.child_process_count}",
                f"tasks={self.asyncio_task_count}",
                f"cpu={self.cpu_user_seconds + self.cpu_system_seconds:.1f}s (+{self.cpu_children_seconds:.1f}s children)",
                f"stage={self.stage or 'unknown'}",
                f"last_heartbeat={self.seconds_since_heartbeat:.0f}s ago",
            ]
        )
        return "HEALTH " + " | ".join(parts)


@dataclass
class HealthEvent:
    """A non-periodic entry (startup, signal, stall, exit, ...)."""

    timestamp: str = ""
    category: str = ""
    message: str = ""
    detail: str = ""


@dataclass
class StartupInfo:
    """Static information about the run, recorded once at startup."""

    timestamp: str = ""
    pid: int = 0
    parent_pid: int = 0
    command_line: str = ""
    working_directory: str = ""
    python_version: str = ""
    platform: str = ""
    auto_coder_version: str = ""
    cgroup_limit_mb: float = 0.0
    system_total_mb: float = 0.0
    max_open_files: int = 0
    health_interval_seconds: float = 0.0
    stall_threshold_seconds: float = 0.0


def _now_iso() -> str:
    """Return the current UTC time in ISO format."""

    return datetime.now(timezone.utc).isoformat()


def _read_proc_status_kb() -> dict:
    """Return the /proc/self/status entries expressed in kB."""

    values: dict = {}
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as handle:
            for line in handle:
                key, _, raw = line.partition(":")
                raw = raw.strip()
                if raw.endswith(" kB"):
                    try:
                        values[key] = float(raw[:-3].strip())
                    except ValueError:
                        continue
                elif key == "Threads":
                    try:
                        values[key] = float(raw)
                    except ValueError:
                        continue
    except OSError:
        return {}
    return values


def _read_meminfo_kb() -> dict:
    """Return the /proc/meminfo entries expressed in kB."""

    values: dict = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                key, _, raw = line.partition(":")
                raw = raw.strip().removesuffix(" kB")
                try:
                    values[key] = float(raw)
                except ValueError:
                    continue
    except OSError:
        return {}
    return values


def _read_first_int(path: str) -> Optional[int]:
    """Read a file containing a single integer, returning None on failure."""

    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read().strip()
    except OSError:
        return None
    if not raw or raw == "max":
        return None
    try:
        return int(raw.split()[0])
    except ValueError:
        return None


def _read_cgroup_memory() -> tuple:
    """Return ``(limit_mb, usage_mb, oom_kill_count)`` for the current cgroup.

    Containers are usually killed against the cgroup limit rather than against
    the total memory of the host, so this is the number that matters when a run
    disappears without a Python traceback.
    """

    limit_bytes = _read_first_int("/sys/fs/cgroup/memory.max")
    usage_bytes = _read_first_int("/sys/fs/cgroup/memory.current")
    events_path = "/sys/fs/cgroup/memory.events"
    if limit_bytes is None and usage_bytes is None:
        # cgroup v1 layout
        limit_bytes = _read_first_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
        usage_bytes = _read_first_int("/sys/fs/cgroup/memory/memory.usage_in_bytes")
        events_path = "/sys/fs/cgroup/memory/memory.oom_control"

    oom_kills = 0
    try:
        with open(events_path, "r", encoding="utf-8") as handle:
            for line in handle:
                key, _, raw = line.partition(" ")
                if key == "oom_kill":
                    try:
                        oom_kills = int(raw.strip())
                    except ValueError:
                        oom_kills = 0
    except OSError:
        oom_kills = 0

    # A "no limit" cgroup reports a huge sentinel value; treat it as unlimited.
    limit_mb = 0.0
    if limit_bytes is not None and 0 < limit_bytes < (1 << 62):
        limit_mb = limit_bytes / _BYTES_PER_MB
    usage_mb = (usage_bytes or 0) / _BYTES_PER_MB
    return limit_mb, usage_mb, oom_kills


def _count_open_fds() -> int:
    """Return the number of open file descriptors (-1 when unavailable)."""

    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return -1


def _count_child_processes() -> int:
    """Return the number of direct child processes (-1 when unavailable)."""

    total = 0
    try:
        task_ids = os.listdir("/proc/self/task")
    except OSError:
        return -1
    for task_id in task_ids:
        try:
            with open(f"/proc/self/task/{task_id}/children", "r", encoding="utf-8") as handle:
                total += len(handle.read().split())
        except OSError:
            continue
    return total


def _count_asyncio_tasks() -> int:
    """Return the number of pending asyncio tasks of the monitored loop."""

    loop = _MONITORED_LOOP
    if loop is None:
        return 0
    try:
        import asyncio

        return len([task for task in asyncio.all_tasks(loop) if not task.done()])
    except Exception:  # pragma: no cover - defensive
        return -1


def _installed_version() -> str:
    """Return the installed auto-coder version, or an empty string."""

    try:
        from importlib.metadata import version

        return version("auto-coder")
    except Exception:  # pragma: no cover - version metadata is optional
        return ""


# Event loop observed for the asyncio task count.
_MONITORED_LOOP: Optional["AbstractEventLoop"] = None


def set_monitored_event_loop(loop: "AbstractEventLoop") -> None:
    """Remember the asyncio loop whose task count should be reported."""

    global _MONITORED_LOOP
    _MONITORED_LOOP = loop


def collect_resource_snapshot(
    reason: str = "periodic",
    start_time: Optional[float] = None,
    baseline_rss_mb: float = 0.0,
    stage: str = "",
    seconds_since_heartbeat: float = 0.0,
) -> ResourceSnapshot:
    """Collect a :class:`ResourceSnapshot` for the current process."""

    snapshot = ResourceSnapshot(
        timestamp=_now_iso(),
        reason=reason,
        pid=os.getpid(),
        uptime_seconds=(time.monotonic() - start_time) if start_time is not None else 0.0,
        stage=stage,
        seconds_since_heartbeat=seconds_since_heartbeat,
    )

    status = _read_proc_status_kb()
    if status:
        snapshot.rss_mb = status.get("VmRSS", 0.0) / 1024.0
        snapshot.peak_rss_mb = status.get("VmHWM", 0.0) / 1024.0
        snapshot.vms_mb = status.get("VmSize", 0.0) / 1024.0
        snapshot.thread_count = int(status.get("Threads", 0.0))

    usage_self = resource.getrusage(resource.RUSAGE_SELF)
    usage_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    snapshot.cpu_user_seconds = usage_self.ru_utime
    snapshot.cpu_system_seconds = usage_self.ru_stime
    snapshot.cpu_children_seconds = usage_children.ru_utime + usage_children.ru_stime
    snapshot.major_page_faults = int(usage_self.ru_majflt)
    if snapshot.peak_rss_mb == 0.0:
        # ru_maxrss is reported in kB on Linux and in bytes on macOS.
        divisor = 1024.0 if sys.platform != "darwin" else _BYTES_PER_MB
        snapshot.peak_rss_mb = usage_self.ru_maxrss / divisor
    if snapshot.rss_mb == 0.0:
        snapshot.rss_mb = snapshot.peak_rss_mb

    meminfo = _read_meminfo_kb()
    if meminfo:
        snapshot.system_total_mb = meminfo.get("MemTotal", 0.0) / 1024.0
        snapshot.system_available_mb = meminfo.get("MemAvailable", 0.0) / 1024.0
        if snapshot.system_total_mb > 0:
            snapshot.system_available_percent = 100.0 * snapshot.system_available_mb / snapshot.system_total_mb
        snapshot.swap_used_mb = (meminfo.get("SwapTotal", 0.0) - meminfo.get("SwapFree", 0.0)) / 1024.0

    limit_mb, usage_mb, oom_kills = _read_cgroup_memory()
    snapshot.cgroup_limit_mb = limit_mb
    snapshot.cgroup_usage_mb = usage_mb
    snapshot.cgroup_oom_kill_count = oom_kills
    if limit_mb > 0:
        snapshot.cgroup_usage_percent = 100.0 * usage_mb / limit_mb

    snapshot.python_thread_count = threading.active_count()
    if snapshot.thread_count == 0:
        snapshot.thread_count = snapshot.python_thread_count
    snapshot.open_fd_count = _count_open_fds()
    snapshot.child_process_count = _count_child_processes()
    snapshot.asyncio_task_count = _count_asyncio_tasks()
    snapshot.gc_generation_counts = "/".join(str(count) for count in gc.get_count())
    if _env_flag("AUTO_CODER_HEALTH_DEEP"):
        snapshot.gc_tracked_objects = len(gc.get_objects())

    if baseline_rss_mb > 0:
        snapshot.rss_growth_mb = snapshot.rss_mb - baseline_rss_mb

    try:
        snapshot.load_average_1m = os.getloadavg()[0]
    except (OSError, AttributeError):  # pragma: no cover - platform dependent
        snapshot.load_average_1m = 0.0

    try:
        snapshot.disk_free_mb = shutil.disk_usage(os.getcwd()).free / _BYTES_PER_MB
    except OSError:  # pragma: no cover - defensive
        snapshot.disk_free_mb = 0.0

    return snapshot


def format_all_thread_stacks() -> str:
    """Return the current stack of every Python thread as text."""

    names = {thread.ident: thread.name for thread in threading.enumerate()}
    blocks: List[str] = []
    for thread_id, frame in sys._current_frames().items():
        name = names.get(thread_id, "unknown")
        stack = "".join(traceback.format_stack(frame))
        blocks.append(f"--- Thread {name} ({thread_id}) ---\n{stack}")
    return "\n".join(blocks)


class HealthMonitor:
    """Background sampler that records resource usage and liveness."""

    _instance: Optional["HealthMonitor"] = None
    _instance_lock = threading.Lock()

    def __init__(
        self,
        log_dir: Optional[Path] = None,
        interval_seconds: Optional[float] = None,
        stall_seconds: Optional[float] = None,
        retention_days: Optional[int] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        self.log_dir = Path(log_dir) if log_dir is not None else get_health_log_dir()
        self.interval_seconds = interval_seconds if interval_seconds is not None else _env_float("AUTO_CODER_HEALTH_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)
        self.stall_seconds = stall_seconds if stall_seconds is not None else _env_float("AUTO_CODER_HEALTH_STALL_SECONDS", DEFAULT_STALL_SECONDS)
        self.retention_days = retention_days if retention_days is not None else _env_int("AUTO_CODER_HEALTH_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)
        self.enabled = enabled if enabled is not None else _env_flag("AUTO_CODER_HEALTH_LOG_ENABLED", True)

        self.start_time = time.monotonic()
        self.baseline_rss_mb = 0.0
        self._stage = "startup"
        self._stage_detail = ""
        self._last_heartbeat = time.monotonic()
        self._stall_reported = False
        self._peak_reported_rss_mb = 0.0
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @classmethod
    def get_instance(cls) -> "HealthMonitor":
        """Return the process-wide monitor instance."""

        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Drop the singleton (used by tests)."""

        with cls._instance_lock:
            if cls._instance is not None:
                cls._instance.stop("reset")
            cls._instance = None

    # ------------------------------------------------------------------
    # File handling
    # ------------------------------------------------------------------
    @property
    def jsonl_path(self) -> Path:
        """Return today's JSON Lines file path."""

        return self.log_dir / f"health-{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"

    def _write_record(self, record_type: str, payload: dict) -> None:
        """Append a single JSON record to the health file."""

        if not self.enabled:
            return
        record = {"record_type": record_type}
        record.update(payload)
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            path = self.jsonl_path
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:  # pragma: no cover - defensive
            logger.warning(f"Failed to write health record: {exc}")

    def _prune_old_files(self) -> None:
        """Delete health files older than the retention window."""

        if not self.enabled or self.retention_days <= 0:
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        try:
            candidates = list(self.log_dir.glob("health-*.jsonl"))
        except OSError:  # pragma: no cover - defensive
            return
        for path in candidates:
            try:
                if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < cutoff:
                    path.unlink()
            except OSError:  # pragma: no cover - defensive
                continue

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def record_event(self, category: str, message: str, detail: str = "") -> None:
        """Record a non-periodic health event."""

        event = HealthEvent(timestamp=_now_iso(), category=category, message=message, detail=detail)
        self._write_record("event", asdict(event))

    def heartbeat(self, stage: str, detail: str = "") -> None:
        """Report that the automation is still making progress.

        Args:
            stage: Coarse description of what the process is doing
                (e.g. ``"producer:fetch-candidates"``).
            detail: Optional extra context such as the issue or PR number.
        """

        with self._state_lock:
            self._stage = stage
            self._stage_detail = detail
            self._last_heartbeat = time.monotonic()
            was_stalled = self._stall_reported
            self._stall_reported = False
        if was_stalled:
            logger.info(f"Heartbeat resumed at stage '{stage}' {detail}".strip())
            self.record_event("stall_recovered", stage, detail)

    def snapshot(self, reason: str = "periodic") -> ResourceSnapshot:
        """Collect a snapshot without writing it anywhere."""

        with self._state_lock:
            stage = self._stage if not self._stage_detail else f"{self._stage} ({self._stage_detail})"
            since_heartbeat = time.monotonic() - self._last_heartbeat
        return collect_resource_snapshot(
            reason=reason,
            start_time=self.start_time,
            baseline_rss_mb=self.baseline_rss_mb,
            stage=stage,
            seconds_since_heartbeat=since_heartbeat,
        )

    def log_snapshot(self, reason: str = "periodic") -> ResourceSnapshot:
        """Collect a snapshot, log it and append it to the JSONL file."""

        snapshot = self.snapshot(reason=reason)
        if self.baseline_rss_mb == 0.0:
            self.baseline_rss_mb = snapshot.rss_mb
        logger.info(snapshot.format_summary())
        self._write_record("snapshot", asdict(snapshot))
        self._log_tracemalloc_top()
        self._check_thresholds(snapshot)
        return snapshot

    def start(self) -> None:
        """Start the background sampling thread (idempotent)."""

        if not self.enabled:
            logger.debug("Health monitoring disabled via AUTO_CODER_HEALTH_LOG_ENABLED")
            return
        if self._thread is not None and self._thread.is_alive():
            return

        self.start_time = time.monotonic()
        self._last_heartbeat = time.monotonic()
        self._stop_event.clear()
        self._prune_old_files()
        self._start_tracemalloc()
        self._record_startup_info()
        self.baseline_rss_mb = 0.0
        self.log_snapshot(reason="startup")

        self._thread = threading.Thread(target=self._run, name="auto-coder-health", daemon=True)
        self._thread.start()
        logger.info(f"Health monitoring started (interval={self.interval_seconds:.0f}s, stall threshold={self.stall_seconds:.0f}s, file={self.jsonl_path})")

    def stop(self, reason: str = "shutdown") -> None:
        """Stop the sampling thread and record a final snapshot."""

        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None
        if self.enabled:
            self.log_snapshot(reason=reason)

    def dump_all_thread_stacks(self, reason: str) -> None:
        """Log the stack of every thread and store it in the dump file."""

        stacks = format_all_thread_stacks()
        logger.warning(f"Thread stack dump ({reason}):\n{stacks}")
        self.record_event("thread_dump", reason, stacks)
        dump_file = _get_diagnostics_file()
        if dump_file is not None:
            try:
                dump_file.write(f"\n===== {_now_iso()} thread dump ({reason}) =====\n")
                dump_file.flush()
                faulthandler.dump_traceback(file=dump_file, all_threads=True)
                dump_file.flush()
            except (OSError, ValueError, RuntimeError):  # pragma: no cover - defensive
                pass

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _record_startup_info(self) -> None:
        """Write the static description of this run."""

        limit_mb, _, _ = _read_cgroup_memory()
        meminfo = _read_meminfo_kb()
        try:
            soft_fd_limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
        except (ValueError, OSError):  # pragma: no cover - defensive
            soft_fd_limit = 0
        info = StartupInfo(
            timestamp=_now_iso(),
            pid=os.getpid(),
            parent_pid=os.getppid(),
            command_line=" ".join(sys.argv),
            working_directory=os.getcwd(),
            python_version=platform.python_version(),
            platform=platform.platform(),
            auto_coder_version=_installed_version(),
            cgroup_limit_mb=limit_mb,
            system_total_mb=meminfo.get("MemTotal", 0.0) / 1024.0,
            max_open_files=int(soft_fd_limit),
            health_interval_seconds=self.interval_seconds,
            stall_threshold_seconds=self.stall_seconds,
        )
        self._write_record("startup", asdict(info))
        logger.info(f"Run started: pid={info.pid} ppid={info.parent_pid} version={info.auto_coder_version or 'unknown'} python={info.python_version} cgroup_limit={info.cgroup_limit_mb:.0f}MB fd_limit={info.max_open_files}")

    def _start_tracemalloc(self) -> None:
        """Enable tracemalloc when explicitly requested."""

        if not _env_flag("AUTO_CODER_HEALTH_TRACEMALLOC"):
            return
        import tracemalloc

        if not tracemalloc.is_tracing():
            tracemalloc.start(10)
            logger.info("tracemalloc enabled for health monitoring")

    def _log_tracemalloc_top(self, limit: int = 10) -> None:
        """Log the largest allocation sites when tracemalloc is active."""

        if not _env_flag("AUTO_CODER_HEALTH_TRACEMALLOC"):
            return
        import tracemalloc

        if not tracemalloc.is_tracing():
            return
        snapshot = tracemalloc.take_snapshot()
        lines = [f"{index}. {stat}" for index, stat in enumerate(snapshot.statistics("lineno")[:limit], start=1)]
        text = "\n".join(lines)
        logger.info(f"Top {limit} allocation sites:\n{text}")
        self.record_event("tracemalloc_top", f"top {limit} allocation sites", text)

    def _check_thresholds(self, snapshot: ResourceSnapshot) -> None:
        """Warn about the conditions that typically end a long run."""

        if snapshot.cgroup_limit_mb > 0 and snapshot.cgroup_usage_percent >= 90.0:
            logger.warning(f"Container memory usage at {snapshot.cgroup_usage_percent:.1f}% of the cgroup limit ({snapshot.cgroup_usage_mb:.0f}/{snapshot.cgroup_limit_mb:.0f}MB); the process may be OOM-killed")
            self.record_event("memory_pressure", "cgroup usage above 90%", snapshot.format_summary())
        elif snapshot.system_total_mb > 0 and snapshot.system_available_percent <= 10.0:
            logger.warning(f"System memory is nearly exhausted: {snapshot.system_available_mb:.0f}MB available ({snapshot.system_available_percent:.1f}%); the process may be OOM-killed")
            self.record_event("memory_pressure", "system available memory below 10%", snapshot.format_summary())

        if snapshot.cgroup_oom_kill_count:
            logger.warning(f"The cgroup reports {snapshot.cgroup_oom_kill_count} OOM kill(s); a previous child process or run was killed by the kernel")

        # Report every doubling of the resident set so leaks are visible.
        if self.baseline_rss_mb > 0 and snapshot.rss_mb >= max(2.0 * self.baseline_rss_mb, self._peak_reported_rss_mb * 1.5, 512.0):
            self._peak_reported_rss_mb = snapshot.rss_mb
            logger.warning(f"Resident memory grew to {snapshot.rss_mb:.1f}MB from a {self.baseline_rss_mb:.1f}MB baseline after {snapshot.uptime_seconds / 3600.0:.2f}h")
            self.record_event("memory_growth", f"rss {snapshot.rss_mb:.1f}MB", snapshot.format_summary())

        if snapshot.disk_free_mb and snapshot.disk_free_mb < 512.0:
            logger.warning(f"Low disk space: {snapshot.disk_free_mb:.0f}MB free on the working directory filesystem")
            self.record_event("disk_pressure", f"{snapshot.disk_free_mb:.0f}MB free", "")

    def _check_stall(self, snapshot: ResourceSnapshot) -> None:
        """Report a hang when no heartbeat arrived within the threshold."""

        if snapshot.seconds_since_heartbeat < self.stall_seconds or self._stall_reported:
            return
        self._stall_reported = True
        logger.warning(f"No heartbeat for {snapshot.seconds_since_heartbeat / 60.0:.1f} minutes (stage: {snapshot.stage or 'unknown'}); dumping thread stacks")
        self.record_event("stall", f"no heartbeat for {snapshot.seconds_since_heartbeat:.0f}s", snapshot.format_summary())
        self.dump_all_thread_stacks("stall")

    def _run(self) -> None:
        """Body of the sampling thread."""

        while not self._stop_event.wait(self.interval_seconds):
            try:
                snapshot = self.log_snapshot(reason="periodic")
                self._check_stall(snapshot)
            except Exception as exc:  # pragma: no cover - the monitor must never kill the run
                logger.warning(f"Health monitor iteration failed: {exc}")


def get_health_monitor() -> HealthMonitor:
    """Return the process-wide :class:`HealthMonitor`."""

    return HealthMonitor.get_instance()


def heartbeat(stage: str, detail: str = "") -> None:
    """Convenience wrapper around :meth:`HealthMonitor.heartbeat`."""

    HealthMonitor.get_instance().heartbeat(stage, detail)


# ----------------------------------------------------------------------
# Crash diagnostics
# ----------------------------------------------------------------------
_diagnostics_file: Optional[IO[str]] = None
_diagnostics_installed = False
_original_excepthook = sys.excepthook


def _get_diagnostics_file() -> Optional[IO[str]]:
    """Return the open diagnostics dump file, if diagnostics are installed."""

    return _diagnostics_file


def _open_diagnostics_file() -> Optional[IO[str]]:
    """Open (or create) the per-process diagnostics dump file."""

    log_dir = get_health_log_dir()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"diagnostics-{os.getpid()}.log"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8")
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning(f"Failed to open diagnostics dump file: {exc}")
        return None


def _handle_fatal_signal(signum: int, frame: Optional[FrameType]) -> None:
    """Log a terminating signal, then let the default behaviour happen."""

    monitor = HealthMonitor.get_instance()
    name = signal.Signals(signum).name
    logger.warning(f"Received signal {name} ({signum}); the process is terminating")
    try:
        monitor.record_event("signal", name, f"stack:\n{''.join(traceback.format_stack(frame))}")
        monitor.log_snapshot(reason=f"signal:{name}")
    except Exception:  # pragma: no cover - never block termination
        pass

    if signum == signal.SIGINT:
        raise KeyboardInterrupt
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def _handle_uncaught_exception(exc_type: type, exc_value: BaseException, exc_traceback: Optional[object]) -> None:
    """Log an exception that escaped to the interpreter."""

    if issubclass(exc_type, KeyboardInterrupt):
        logger.info("Interrupted by user (KeyboardInterrupt)")
    else:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))  # type: ignore[arg-type]
        logger.critical(f"Uncaught exception terminated the process:\n{text}")
        try:
            monitor = HealthMonitor.get_instance()
            monitor.record_event("uncaught_exception", f"{exc_type.__name__}: {exc_value}", text)
            monitor.log_snapshot(reason="uncaught_exception")
        except Exception:  # pragma: no cover - defensive
            pass
    _original_excepthook(exc_type, exc_value, exc_traceback)  # type: ignore[arg-type]


def _handle_thread_exception(args: threading.ExceptHookArgs) -> None:
    """Log an exception that killed a thread."""

    text = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))  # type: ignore[arg-type]
    thread_name = args.thread.name if args.thread is not None else "unknown"
    logger.error(f"Uncaught exception in thread '{thread_name}':\n{text}")
    try:
        HealthMonitor.get_instance().record_event("thread_exception", f"{thread_name}: {args.exc_value}", text)
    except Exception:  # pragma: no cover - defensive
        pass


def _handle_exit() -> None:
    """Record that the interpreter is shutting down."""

    try:
        monitor = HealthMonitor.get_instance()
        monitor.record_event("exit", "interpreter shutdown", f"uptime={time.monotonic() - monitor.start_time:.0f}s")
        monitor.log_snapshot(reason="exit")
    except Exception:  # pragma: no cover - defensive
        pass


def install_crash_diagnostics() -> None:
    """Install signal, exception and exit handlers (idempotent).

    ``faulthandler`` is enabled so that a hard crash (segfault, abort inside a
    native extension) still leaves a stack trace behind, and ``SIGUSR1`` dumps
    the stacks of all threads on demand::

        kill -USR1 <pid>
    """

    global _diagnostics_file, _diagnostics_installed

    if _diagnostics_installed:
        return
    _diagnostics_installed = True

    _diagnostics_file = _open_diagnostics_file()
    if _diagnostics_file is not None:
        try:
            faulthandler.enable(file=_diagnostics_file, all_threads=True)
        except (RuntimeError, ValueError, AttributeError):  # pragma: no cover - defensive
            logger.warning("faulthandler could not be enabled")
        if hasattr(faulthandler, "register") and hasattr(signal, "SIGUSR1"):
            try:
                # chain=False: the default action for SIGUSR1 terminates the
                # process, and this signal must only dump the stacks of a run
                # that appears to be stuck.
                faulthandler.register(signal.SIGUSR1, file=_diagnostics_file, all_threads=True, chain=False)
            except (RuntimeError, ValueError, OSError):  # pragma: no cover - defensive
                pass

    for signal_name in ("SIGTERM", "SIGHUP", "SIGINT", "SIGQUIT"):
        signal_number = getattr(signal, signal_name, None)
        if signal_number is None:
            continue
        try:
            signal.signal(signal_number, _handle_fatal_signal)
        except (ValueError, OSError):  # pragma: no cover - not in main thread
            continue

    sys.excepthook = _handle_uncaught_exception
    threading.excepthook = _handle_thread_exception
    atexit.register(_handle_exit)
    logger.debug(f"Crash diagnostics installed (dump file: {get_health_log_dir()}/diagnostics-{os.getpid()}.log)")


def install_asyncio_diagnostics(loop: "AbstractEventLoop") -> None:
    """Log unhandled asyncio exceptions and slow callbacks for ``loop``."""

    set_monitored_event_loop(loop)

    def _handler(_loop: "AbstractEventLoop", context: dict) -> None:
        message = str(context.get("message", "unhandled asyncio error"))
        exception = context.get("exception")
        if isinstance(exception, BaseException):
            text = "".join(traceback.format_exception(type(exception), exception, exception.__traceback__))
            logger.error(f"Unhandled asyncio exception: {message}\n{text}")
            detail = text
        else:
            logger.error(f"Unhandled asyncio error: {message} ({context})")
            detail = str(context)
        try:
            HealthMonitor.get_instance().record_event("asyncio_error", message, detail)
        except Exception:  # pragma: no cover - defensive
            pass

    try:
        loop.set_exception_handler(_handler)
    except AttributeError:  # pragma: no cover - defensive
        pass


def start_health_monitoring() -> HealthMonitor:
    """Install crash diagnostics and start the background monitor."""

    install_crash_diagnostics()
    monitor = HealthMonitor.get_instance()
    monitor.start()
    return monitor

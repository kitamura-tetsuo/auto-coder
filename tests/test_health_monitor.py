"""Tests for the runtime health monitor and crash diagnostics."""

import json
import os
import signal
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from src.auto_coder.cli_commands_health import health
from src.auto_coder.health_monitor import (
    HealthMonitor,
    ResourceSnapshot,
    collect_resource_snapshot,
    format_all_thread_stacks,
    get_health_log_dir,
    heartbeat,
)


@pytest.fixture
def monitor(tmp_path):
    """Return a monitor writing into a temporary directory."""

    HealthMonitor.reset_instance()
    instance = HealthMonitor(log_dir=tmp_path, interval_seconds=0.05, stall_seconds=0.2, retention_days=14, enabled=True)
    HealthMonitor._instance = instance
    yield instance
    instance.stop("test-teardown")
    HealthMonitor.reset_instance()


def _read_records(path: Path):
    """Return the parsed records of a health JSONL file (empty when absent)."""

    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _age_heartbeat(monitor: HealthMonitor, seconds: float) -> None:
    """Pretend the last heartbeat happened ``seconds`` ago.

    ``time.sleep`` is mocked globally by the test suite, so the heartbeat age is
    moved directly instead of waiting.
    """

    monitor._last_heartbeat = time.monotonic() - seconds


class TestResourceSnapshot:
    def test_snapshot_reports_process_memory_and_counters(self):
        snapshot = collect_resource_snapshot(reason="unit-test", start_time=time.monotonic() - 5.0, baseline_rss_mb=1.0)

        assert snapshot.reason == "unit-test"
        assert snapshot.pid == os.getpid()
        assert snapshot.uptime_seconds >= 5.0
        assert snapshot.rss_mb > 0.0
        assert snapshot.peak_rss_mb >= snapshot.rss_mb - 0.001
        assert snapshot.rss_growth_mb == pytest.approx(snapshot.rss_mb - 1.0)
        assert snapshot.thread_count >= 1
        assert snapshot.python_thread_count >= 1
        assert snapshot.cpu_user_seconds > 0.0
        assert snapshot.timestamp

    @pytest.mark.skipif(not Path("/proc/meminfo").exists(), reason="requires /proc")
    def test_snapshot_reports_system_memory(self):
        snapshot = collect_resource_snapshot()

        assert snapshot.system_total_mb > 0.0
        assert snapshot.system_available_mb > 0.0
        assert 0.0 < snapshot.system_available_percent <= 100.0
        assert snapshot.open_fd_count > 0

    def test_format_summary_contains_memory_and_stage(self):
        snapshot = ResourceSnapshot(reason="periodic", rss_mb=123.0, peak_rss_mb=200.0, uptime_seconds=7200.0, stage="worker-0:processing", cgroup_limit_mb=1000.0, cgroup_usage_mb=500.0, cgroup_usage_percent=50.0)

        summary = snapshot.format_summary()

        assert summary.startswith("HEALTH ")
        assert "rss=123.0MB" in summary
        assert "peak 200.0MB" in summary
        assert "uptime=2.00h" in summary
        assert "cgroup=500/1000MB (50.0%)" in summary
        assert "stage=worker-0:processing" in summary


class TestHealthMonitorLogging:
    def test_log_snapshot_appends_json_record(self, monitor, tmp_path):
        monitor.log_snapshot(reason="startup")

        records = _read_records(monitor.jsonl_path)
        assert len(records) == 1
        assert records[0]["record_type"] == "snapshot"
        assert records[0]["reason"] == "startup"
        assert records[0]["rss_mb"] > 0.0
        assert records[0]["pid"] == os.getpid()

    def test_log_file_permissions_are_restricted(self, monitor):
        monitor.record_event("test", "permission check")

        assert (os.stat(monitor.jsonl_path).st_mode & 0o777) == 0o600

    def test_record_event_appends_event_record(self, monitor):
        monitor.record_event("signal", "SIGTERM", "detail text")

        records = _read_records(monitor.jsonl_path)
        assert records[0]["record_type"] == "event"
        assert records[0]["category"] == "signal"
        assert records[0]["message"] == "SIGTERM"
        assert records[0]["detail"] == "detail text"

    def test_start_records_startup_info_and_first_snapshot(self, monitor):
        monitor.start()
        monitor.stop("test")

        records = _read_records(monitor.jsonl_path)
        types = [record["record_type"] for record in records]
        assert types[0] == "startup"
        assert types[1] == "snapshot"
        assert records[0]["pid"] == os.getpid()
        assert records[0]["command_line"]
        assert records[0]["health_interval_seconds"] == 0.05
        assert records[-1]["reason"] == "test"

    def test_background_thread_writes_periodic_snapshots(self, monitor):
        monitor.start()
        deadline = time.time() + 5.0
        while time.time() < deadline:
            periodic = [record for record in _read_records(monitor.jsonl_path) if record["record_type"] == "snapshot" and record["reason"] == "periodic"]
            if len(periodic) >= 2:
                break
            time.sleep(0.05)
        monitor.stop("test")

        periodic = [record for record in _read_records(monitor.jsonl_path) if record["record_type"] == "snapshot" and record["reason"] == "periodic"]
        assert len(periodic) >= 2

    def test_disabled_monitor_writes_nothing(self, tmp_path):
        disabled = HealthMonitor(log_dir=tmp_path, enabled=False)

        disabled.record_event("test", "ignored")
        disabled.start()

        assert list(tmp_path.glob("health-*.jsonl")) == []
        assert disabled._thread is None

    def test_prune_removes_files_older_than_retention(self, tmp_path):
        stale = tmp_path / "health-2000-01-01.jsonl"
        stale.write_text("{}\n", encoding="utf-8")
        old_time = time.time() - (30 * 24 * 3600)
        os.utime(stale, (old_time, old_time))
        fresh = tmp_path / "health-2000-01-02.jsonl"
        fresh.write_text("{}\n", encoding="utf-8")

        HealthMonitor(log_dir=tmp_path, retention_days=14, enabled=True)._prune_old_files()

        assert not stale.exists()
        assert fresh.exists()


class TestHeartbeatAndStall:
    def test_heartbeat_updates_stage_in_snapshot(self, monitor):
        monitor.heartbeat("worker-1:processing", "pr #42")

        snapshot = monitor.snapshot()
        assert snapshot.stage == "worker-1:processing (pr #42)"
        assert snapshot.seconds_since_heartbeat < 1.0

    def test_module_level_heartbeat_targets_singleton(self, monitor):
        heartbeat("producer:get-candidates")

        assert monitor.snapshot().stage == "producer:get-candidates"

    def test_stall_is_reported_once_with_thread_dump(self, monitor):
        monitor.heartbeat("worker-0:processing", "issue #7")
        _age_heartbeat(monitor, 5.0)

        monitor._check_stall(monitor.snapshot())
        monitor._check_stall(monitor.snapshot())

        stalls = [record for record in _read_records(monitor.jsonl_path) if record.get("category") == "stall"]
        dumps = [record for record in _read_records(monitor.jsonl_path) if record.get("category") == "thread_dump"]
        assert len(stalls) == 1
        assert len(dumps) == 1
        assert "Thread" in dumps[0]["detail"]

    def test_heartbeat_after_stall_records_recovery(self, monitor):
        monitor.heartbeat("worker-0:processing")
        _age_heartbeat(monitor, 5.0)
        monitor._check_stall(monitor.snapshot())

        monitor.heartbeat("worker-0:idle")

        categories = [record.get("category") for record in _read_records(monitor.jsonl_path)]
        assert "stall_recovered" in categories

    def test_no_stall_reported_while_heartbeats_arrive(self, monitor):
        monitor.heartbeat("producer:sleep")

        monitor._check_stall(monitor.snapshot())

        assert [record for record in _read_records(monitor.jsonl_path) if record.get("category") == "stall"] == []


class TestThresholdWarnings:
    def test_memory_pressure_event_on_high_cgroup_usage(self, monitor):
        monitor._check_thresholds(ResourceSnapshot(cgroup_limit_mb=1000.0, cgroup_usage_mb=950.0, cgroup_usage_percent=95.0))

        records = _read_records(monitor.jsonl_path)
        assert records[0]["category"] == "memory_pressure"
        assert records[0]["message"] == "cgroup usage above 90%"

    def test_memory_pressure_event_on_low_system_memory(self, monitor):
        monitor._check_thresholds(ResourceSnapshot(system_total_mb=8000.0, system_available_mb=400.0, system_available_percent=5.0))

        records = _read_records(monitor.jsonl_path)
        assert records[0]["category"] == "memory_pressure"
        assert records[0]["message"] == "system available memory below 10%"

    def test_memory_growth_event_when_rss_doubles(self, monitor):
        monitor.baseline_rss_mb = 400.0

        monitor._check_thresholds(ResourceSnapshot(rss_mb=900.0, uptime_seconds=7200.0))

        records = _read_records(monitor.jsonl_path)
        assert records[0]["category"] == "memory_growth"
        assert "900.0MB" in records[0]["message"]

    def test_no_growth_event_for_stable_memory(self, monitor):
        monitor.baseline_rss_mb = 400.0

        monitor._check_thresholds(ResourceSnapshot(rss_mb=420.0, system_total_mb=8000.0, system_available_mb=6000.0, system_available_percent=75.0, disk_free_mb=10000.0))

        assert _read_records(monitor.jsonl_path) == []

    def test_low_disk_space_event(self, monitor):
        monitor._check_thresholds(ResourceSnapshot(disk_free_mb=100.0))

        categories = [record["category"] for record in _read_records(monitor.jsonl_path)]
        assert "disk_pressure" in categories


class TestThreadStacks:
    def test_format_all_thread_stacks_includes_current_thread(self):
        stacks = format_all_thread_stacks()

        assert f"({threading.get_ident()})" in stacks
        assert "test_format_all_thread_stacks_includes_current_thread" in stacks


class TestHealthLogDir:
    def test_log_dir_follows_environment_variable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AUTO_CODER_HEALTH_LOG_DIR", str(tmp_path / "custom"))

        assert get_health_log_dir() == tmp_path / "custom"

    def test_log_dir_defaults_below_home(self, monkeypatch, tmp_path):
        monkeypatch.delenv("AUTO_CODER_HEALTH_LOG_DIR", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))

        assert get_health_log_dir() == tmp_path / ".auto-coder" / "logs"


class TestSignalDiagnostics:
    def test_sigterm_handler_records_event_and_terminates(self, monitor, monkeypatch):
        from src.auto_coder import health_monitor

        killed = []
        monkeypatch.setattr(health_monitor.signal, "signal", lambda *args: None)
        monkeypatch.setattr(health_monitor.os, "kill", lambda pid, sig: killed.append((pid, sig)))

        health_monitor._handle_fatal_signal(signal.SIGTERM, None)

        assert killed == [(os.getpid(), signal.SIGTERM)]
        records = _read_records(monitor.jsonl_path)
        assert records[0]["category"] == "signal"
        assert records[0]["message"] == "SIGTERM"
        assert records[-1]["reason"] == "signal:SIGTERM"

    def test_sigint_handler_raises_keyboard_interrupt(self, monitor):
        from src.auto_coder import health_monitor

        with pytest.raises(KeyboardInterrupt):
            health_monitor._handle_fatal_signal(signal.SIGINT, None)

        assert [record for record in _read_records(monitor.jsonl_path) if record.get("message") == "SIGINT"]

    def test_uncaught_exception_is_recorded(self, monitor, monkeypatch):
        from src.auto_coder import health_monitor

        monkeypatch.setattr(health_monitor, "_original_excepthook", lambda *args: None)
        error = RuntimeError("boom")

        health_monitor._handle_uncaught_exception(RuntimeError, error, None)

        records = _read_records(monitor.jsonl_path)
        assert records[0]["category"] == "uncaught_exception"
        assert "RuntimeError: boom" in records[0]["message"]


class TestHealthCommand:
    def test_health_command_summarizes_log(self, monitor):
        monitor.start()
        monitor.record_event("signal", "SIGTERM", "terminated")
        monitor.stop("test")

        result = CliRunner().invoke(health, ["--log-dir", str(monitor.log_dir)])

        assert result.exit_code == 0
        assert "Snapshots:" in result.output
        assert "Memory:" in result.output
        assert "[signal] SIGTERM" in result.output

    def test_health_command_reports_missing_logs(self, tmp_path):
        result = CliRunner().invoke(health, ["--log-dir", str(tmp_path / "empty")])

        assert result.exit_code == 0
        assert "No health logs found" in result.output


class TestEngineIntegration:
    """The automation engine must explain why its loops stopped."""

    def _build_engine(self):
        from unittest.mock import MagicMock

        from src.auto_coder.automation_engine import AutomationEngine

        return AutomationEngine(MagicMock())

    def test_start_automation_records_silent_exit(self, monitor):
        import asyncio

        engine = self._build_engine()

        async def _no_op(*args, **kwargs):
            return None

        engine._producer_loop = _no_op
        engine._worker_loop = _no_op

        asyncio.run(engine.start_automation("owner/repo", concurrency=1))

        records = _read_records(monitor.jsonl_path)
        events = [record for record in records if record["record_type"] == "event"]
        assert [event for event in events if event["category"] == "engine_stop" and event["message"] == "all engine tasks exited"]
        assert [record for record in records if record.get("reason") == "engine_stop"]

    def test_start_automation_reports_engine_stage_heartbeat(self, monitor):
        import asyncio

        engine = self._build_engine()
        seen_stages = []

        async def _record_stage(*args, **kwargs):
            seen_stages.append(monitor.snapshot().stage)

        engine._producer_loop = _record_stage
        engine._worker_loop = _record_stage

        asyncio.run(engine.start_automation("owner/repo", concurrency=1))

        assert seen_stages
        assert all(stage.startswith("engine:start") for stage in seen_stages)


class TestCrashDiagnosticsInstallation:
    """Handler installation must not change how the process terminates."""

    def _install_with_stubs(self, monkeypatch, tmp_path):
        from src.auto_coder import health_monitor

        monkeypatch.setenv("AUTO_CODER_HEALTH_LOG_DIR", str(tmp_path))
        monkeypatch.setattr(health_monitor, "_diagnostics_installed", False)
        monkeypatch.setattr(health_monitor, "_diagnostics_file", None)

        registered = {}
        installed_signals = []

        monkeypatch.setattr(health_monitor.faulthandler, "enable", lambda **kwargs: registered.update({"enable": kwargs}))
        monkeypatch.setattr(health_monitor.faulthandler, "register", lambda signum, **kwargs: registered.update({"signum": signum, **kwargs}))
        monkeypatch.setattr(health_monitor.signal, "signal", lambda signum, handler: installed_signals.append(signum))
        monkeypatch.setattr(health_monitor.atexit, "register", lambda func: func)

        health_monitor.install_crash_diagnostics()
        return registered, installed_signals

    def test_sigusr1_dump_does_not_chain_to_default_action(self, monkeypatch, tmp_path):
        registered, _ = self._install_with_stubs(monkeypatch, tmp_path)

        # chain=True would run the default SIGUSR1 action, which kills the process.
        assert registered["signum"] == signal.SIGUSR1
        assert registered["chain"] is False
        assert registered["all_threads"] is True

    def test_termination_signals_are_handled(self, monkeypatch, tmp_path):
        _, installed_signals = self._install_with_stubs(monkeypatch, tmp_path)

        assert set(installed_signals) == {signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGQUIT}

    def test_diagnostics_dump_file_is_created(self, monkeypatch, tmp_path):
        self._install_with_stubs(monkeypatch, tmp_path)

        dump_files = list(tmp_path.glob("diagnostics-*.log"))
        assert len(dump_files) == 1
        assert (os.stat(dump_files[0]).st_mode & 0o777) == 0o600

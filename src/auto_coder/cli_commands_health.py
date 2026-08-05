"""CLI command that summarizes the health logs of previous runs."""

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import click

from .health_monitor import get_health_log_dir
from .logger_config import setup_logger


@dataclass
class HealthLogSummary:
    """Aggregated view of one health JSON Lines file."""

    path: str = ""
    snapshot_count: int = 0
    first_timestamp: str = ""
    last_timestamp: str = ""
    first_rss_mb: float = 0.0
    last_rss_mb: float = 0.0
    peak_rss_mb: float = 0.0
    last_uptime_hours: float = 0.0
    last_stage: str = ""
    cgroup_limit_mb: float = 0.0
    peak_cgroup_usage_percent: float = 0.0
    max_open_fd_count: int = 0
    max_child_process_count: int = 0
    startup_lines: List[str] = field(default_factory=list)
    event_lines: List[str] = field(default_factory=list)


def _load_summary(path: Path, event_limit: int) -> HealthLogSummary:
    """Build a :class:`HealthLogSummary` from a health JSONL file."""

    summary = HealthLogSummary(path=str(path))
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            record_type = record.get("record_type", "")
            timestamp = str(record.get("timestamp", ""))

            if record_type == "startup":
                summary.startup_lines.append(f"{timestamp} pid={record.get('pid')} version={record.get('auto_coder_version') or 'unknown'} cmd={record.get('command_line', '')}")
                summary.cgroup_limit_mb = float(record.get("cgroup_limit_mb", 0.0) or 0.0)
            elif record_type == "event":
                summary.event_lines.append(f"{timestamp} [{record.get('category', '')}] {record.get('message', '')}")
            elif record_type == "snapshot":
                summary.snapshot_count += 1
                rss = float(record.get("rss_mb", 0.0) or 0.0)
                if not summary.first_timestamp:
                    summary.first_timestamp = timestamp
                    summary.first_rss_mb = rss
                summary.last_timestamp = timestamp
                summary.last_rss_mb = rss
                summary.peak_rss_mb = max(summary.peak_rss_mb, float(record.get("peak_rss_mb", 0.0) or 0.0), rss)
                summary.last_uptime_hours = float(record.get("uptime_seconds", 0.0) or 0.0) / 3600.0
                summary.last_stage = str(record.get("stage", ""))
                summary.peak_cgroup_usage_percent = max(summary.peak_cgroup_usage_percent, float(record.get("cgroup_usage_percent", 0.0) or 0.0))
                summary.max_open_fd_count = max(summary.max_open_fd_count, int(record.get("open_fd_count", 0) or 0))
                summary.max_child_process_count = max(summary.max_child_process_count, int(record.get("child_process_count", 0) or 0))

    summary.event_lines = summary.event_lines[-event_limit:]
    summary.startup_lines = summary.startup_lines[-event_limit:]
    return summary


@click.command(name="health")
@click.option("--log-dir", "log_dir_option", help="Directory holding health-*.jsonl files (default: ~/.auto-coder/logs)")
@click.option("--days", default=1, type=int, help="Number of most recent health files to summarize (default: 1)")
@click.option("--events", "event_limit", default=20, type=int, help="Number of recent events to display per file (default: 20)")
def health(log_dir_option: Optional[str], days: int, event_limit: int) -> None:
    """Summarize recorded resource usage and stop reasons of previous runs."""

    setup_logger(stream=sys.stderr)

    log_dir = Path(log_dir_option).expanduser() if log_dir_option else get_health_log_dir()
    files = sorted(log_dir.glob("health-*.jsonl"))
    if not files:
        click.echo(f"No health logs found in {log_dir}")
        return

    for path in files[-max(days, 1) :]:
        summary = _load_summary(path, event_limit=event_limit)
        click.echo(f"\n=== {summary.path} ===")
        click.echo(f"Snapshots: {summary.snapshot_count} ({summary.first_timestamp} -> {summary.last_timestamp})")
        click.echo(f"Memory: {summary.first_rss_mb:.1f}MB -> {summary.last_rss_mb:.1f}MB (peak {summary.peak_rss_mb:.1f}MB)")
        if summary.cgroup_limit_mb:
            click.echo(f"Container limit: {summary.cgroup_limit_mb:.0f}MB (peak usage {summary.peak_cgroup_usage_percent:.1f}%)")
        click.echo(f"Last uptime: {summary.last_uptime_hours:.2f}h, last stage: {summary.last_stage or 'unknown'}")
        click.echo(f"Peak open file descriptors: {summary.max_open_fd_count}, peak child processes: {summary.max_child_process_count}")

        if summary.startup_lines:
            click.echo("Runs:")
            for line in summary.startup_lines:
                click.echo(f"  {line}")

        if summary.event_lines:
            click.echo("Events:")
            for line in summary.event_lines:
                click.echo(f"  {line}")
        else:
            click.echo("Events: none recorded")

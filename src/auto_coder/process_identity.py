"""Conservative Linux process-liveness evidence for durable execution owners."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class ProcessIdentity:
    """OS identity that distinguishes a process from a reused numeric PID."""

    pid: int
    boot_id: str
    start_ticks: int
    state: str = ""


def read_process_identity(pid: int) -> Optional[ProcessIdentity]:
    """Return None when procfs identity evidence cannot be read reliably."""
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        closing_parenthesis = stat.rfind(")")
        if not boot_id or closing_parenthesis < 0:
            return None
        fields = stat[closing_parenthesis + 2 :].split()
        state = fields[0]
        start_ticks = int(fields[19])
        if len(state) != 1:
            return None
        return ProcessIdentity(pid, boot_id, start_ticks, state)
    except (OSError, UnicodeError, ValueError, IndexError):
        return None


def process_is_dead(owner: ProcessIdentity, current: Optional[ProcessIdentity]) -> bool:
    """Require positive evidence of death; unreadable evidence is not expiry."""
    if current is not None:
        return current.boot_id != owner.boot_id or current.start_ticks != owner.start_ticks or current.state in {"Z", "X", "x"}
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        exists = Path(f"/proc/{owner.pid}").exists()
    except (OSError, UnicodeError):
        return False
    return bool(boot_id) and (boot_id != owner.boot_id or not exists)

import collections
import time
import threading
from typing import Dict, List, Any, Optional


class TraceLogger:
    """
    Singleton logger for tracing decision steps and automation activities.
    Stores logs in a circular buffer for lightweight in-memory access.
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, max_len: int = 2000):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(TraceLogger, cls).__new__(cls)
                    cls._instance.logs = collections.deque(maxlen=max_len)
        return cls._instance

    def log(
        self,
        category: str,
        message: str,
        item_type: Optional[str] = None,
        item_number: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Log a trace entry.

        Args:
            category: The category of the log (e.g., "Merge Check", "Decision").
            message: The log message.
            item_type: "pr" or "issue".
            item_number: The PR or Issue number.
            details: Additional structured data.
        """
        entry = {"timestamp": time.time(), "category": category, "message": message, "item_type": item_type, "item_number": item_number, "details": details or {}}
        self.logs.append(entry)

    def get_logs(self, limit: int = 100, item_type: Optional[str] = None, item_number: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Retrieve logs, optionally filtering by item.
        """
        filtered_logs = list(self.logs)

        if item_type and item_number is not None:
            filtered_logs = [log for log in filtered_logs if log["item_type"] == item_type and log["item_number"] == item_number]

        return filtered_logs[-limit:]

    def clear(self) -> None:
        self.logs.clear()


def get_trace_logger() -> TraceLogger:
    """Get the singleton TraceLogger instance."""
    return TraceLogger()
